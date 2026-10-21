"""bot-activity backend — read API for the desktop pane, mounted at /api/plugins/bot-activity/.

Live rows arrive over the ``plugin.bot-activity.changed`` event; these routes serve the
initial backlog, the polling fallback (``since``), the Bot roster, and "clear".
Handlers are plain ``def`` so FastAPI runs the blocking SQLite reads in its threadpool.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

DEFAULT_LIMIT = 300
MAX_LIMIT = 1000
# A running row not updated for this long is treated as abandoned (crashed turn, killed process).
STALE_AFTER_S = 15 * 60
# Room streams have no end frame (turn.settled never reaches plugins): quiet for this long = done.
STREAM_QUIET_S = 30
ACTIVE_STATUSES = ("running", "waiting")


def _load_sibling(filename: str):
    """Import a module from the plugin root by path — this file is loaded standalone by the
    dashboard mount, not as part of the plugin package."""
    name = f"_bot_activity_{Path(filename).stem}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent.parent / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


store = _load_sibling("store.py")
rooms = _load_sibling("rooms.py")
installer = _load_sibling("installer.py")
PACKAGE_DIR = Path(__file__).resolve().parent.parent


def _connect() -> sqlite3.Connection:
    return store.connect(store.default_db_path())


@router.get("/activity")
def activity(
    since: int = Query(0, ge=0, description="Return rows with rev > since"),
    bot: Optional[str] = None,
    room: Optional[str] = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    where, params = ["rev > ?"], [since]
    if bot:
        where.append("bot = ?")
        params.append(bot)
    if room:
        where.append("room_id = ?")
        params.append(room)
    # Initial page: the most RECENT activity (a room repair re-revs old rows, so rev order would
    # surface stale history). Incremental polls walk forward by rev so a page cut at `limit`
    # never skips rows — the client's cursor resumes from the last rev it received.
    order = "updated DESC" if since == 0 else "rev ASC"
    sql = f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?"
    with closing(_connect()) as conn:
        rows = [store.row_to_dict(r) for r in conn.execute(sql, (*params, limit))]
        max_rev = conn.execute("SELECT COALESCE(MAX(rev), 0) FROM events").fetchone()[0]
    if since == 0:
        rows.reverse()
    return {"rows": rows, "rev": max(max_rev, since), "now": time.time()}


@router.get("/bots")
def bots() -> dict[str, Any]:
    """Every known Bot (profiles on disk ∪ Bots seen in the log) with its live work items."""
    now = time.time()
    marks = ",".join("?" for _ in ACTIVE_STATUSES)
    with closing(_connect()) as conn:
        last_seen = {r["bot"]: r["last"] for r in conn.execute(
            "SELECT bot, MAX(updated) AS last FROM events WHERE bot IS NOT NULL GROUP BY bot")}
        active_rows = conn.execute(
            f"""SELECT * FROM events
                WHERE (status IN ({marks}) AND updated > ?) OR (status = 'streaming' AND updated > ?)
                ORDER BY ts""",
            (*ACTIVE_STATUSES, now - STALE_AFTER_S, now - STREAM_QUIET_S)).fetchall()
    active: dict[str, list[dict[str, Any]]] = {}
    for row in active_rows:
        active.setdefault(row["bot"] or "unknown", []).append(
            {k: row[k] for k in ("key", "kind", "status", "title", "ts", "updated", "room_id", "thread_id")})

    names = set(last_seen) | set(active) | set(_profile_names())
    root = _root()
    roster = [{"bot": name, "title": rooms.bot_title(root, name), "last_seen": last_seen.get(name),
               "active": active.get(name, [])}
              for name in names]
    roster.sort(key=lambda b: (not b["active"], -(b["last_seen"] or 0), b["bot"]))
    return {"bots": roster, "now": now}


@router.get("/rooms")
def group_rooms() -> dict[str, Any]:
    """Group Chats known to Desktop plus any room seen in the log (e.g. since-deleted rooms),
    with live work counts so the pane can order and badge its sections."""
    now = time.time()
    with closing(_connect()) as conn:
        stats = {r["room_id"]: dict(r) for r in conn.execute(
            """SELECT room_id, MAX(updated) AS last_seen,
                      SUM(CASE WHEN (status IN ('running', 'waiting') AND updated > ?)
                                 OR (status = 'streaming' AND updated > ?) THEN 1 ELSE 0 END) AS active
               FROM events WHERE room_id IS NOT NULL GROUP BY room_id""",
            (now - STALE_AFTER_S, now - STREAM_QUIET_S))}
    known = {room["room_id"]: room for room in rooms.group_rooms(_root())}
    out = []
    for room_id in set(known) | set(stats):
        room = known.get(room_id, {"room_id": room_id, "name": None, "members": [], "threads": {}})
        stat = stats.get(room_id, {})
        out.append({**room, "last_seen": stat.get("last_seen"), "active": stat.get("active") or 0,
                    "deleted": room_id not in known})
    out.sort(key=lambda r: (-(r["active"] > 0), -(r["last_seen"] or 0)))
    return {"rooms": out, "now": now}


@router.get("/setup")
def setup() -> dict[str, Any]:
    """Which Bot profiles carry an up-to-date, enabled copy of this plugin."""
    root = _root()
    source = installer.source_dir(root, PACKAGE_DIR)
    profiles = [{**status, "title": rooms.bot_title(root, status["bot"])}
                for status in installer.setup_status(root, source)]
    return {
        "profiles": profiles,
        "untracked": [p["bot"] for p in profiles if not p["tracked"] and not p["opted_out"]],
        "outdated": [p["bot"] for p in profiles if p["tracked"] and p["outdated"]],
        "opted_out": [p["bot"] for p in profiles if p["opted_out"]],
    }


@router.post("/setup")
def track(body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Track Bots: copy/refresh the plugin into their profiles and enable it (no restart needed —
    the enable CLI asks this backend to load it live). ``{"bots": [...]}`` limits the targets;
    omitted = every untracked profile."""
    bots = (body or {}).get("bots")
    if bots is not None and (not isinstance(bots, list) or not all(isinstance(b, str) for b in bots)):
        raise HTTPException(status_code=422, detail="bots must be a list of profile names")
    root = _root()
    results = installer.track(root, installer.source_dir(root, PACKAGE_DIR), bots)
    return {"results": results, "ok": all(r["ok"] for r in results)}


@router.post("/clear")
def clear(body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    bot = (body or {}).get("bot")
    if bot is not None and not isinstance(bot, str):
        raise HTTPException(status_code=422, detail="bot must be a string")
    with closing(_connect()) as conn, conn:
        cur = conn.execute("DELETE FROM events WHERE bot = ?", (bot,)) if bot else conn.execute("DELETE FROM events")
    return {"ok": True, "deleted": cur.rowcount}


def _root() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root()


def _profile_names() -> list[str]:
    return installer.profile_names(_root())