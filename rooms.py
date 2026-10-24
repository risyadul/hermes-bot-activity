"""Group Chat attribution: which room/thread a Bot session belongs to, and display names.

Desktop Group Chat runs every member turn in a hidden per-room, per-thread session of the
member's own profile, titled ``Group: <roomId> · <threadId>`` at creation
(apps/desktop/src/plugins/hermes-bots/group-turns.ts). Legacy rooms without a roomId use the
room's display name in place of the id. That title is not durable — sessions get re-titled —
so a session is resolved, in order, by:

  1. the nearest ``Group:`` title on it or its ancestors (subagents → parent_session_id);
  2. its lineage root's opening prompt header ``[Group chat: "<room name at the time>"]``,
     matched to a live room of that name, else to the room of titled sessions that carried
     that header (the room was renamed since). This path yields the room only, never the
     thread, and stays provisional until the session's title lands. The room names and Bot titles live in ``profile.yaml``
``ui_meta`` (``hermes-bots-groups`` on the default profile, ``hermes-bots`` on each profile).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

GROUP_TITLE_PREFIX = "Group: "
GROUP_TITLE_SEPARATOR = " · "
LEGACY_THREAD = "legacy"
# Desktop inserts the raw room name, which may itself contain quotes; anchor on what follows.
GROUP_PROMPT_HEADER = re.compile(r'^\[Group chat: "(?P<name>.*?)"\] You are @')
MAX_PARENT_DEPTH = 8
ALIAS_TTL_S = 60.0
RESOLVE_CACHE_MAX = 4096
# Hermes writes the ``Group:`` title only after a session's FIRST turn completes, so until then
# a session resolves by its prompt header (or not at all). Those answers are provisional: they
# expire after this long so the title — authoritative, cached for good — wins once it lands.
PROVISIONAL_TTL_S = 30.0
STATE_DB_TIMEOUT_S = 1.0
THREAD_TOPIC_CHARS = 90

RoomRef = tuple[str, Optional[str]]


def parse_group_title(title: Any) -> Optional[RoomRef]:
    """``"Group: rm1 · tm2"`` → ``("rm1", "tm2")``; anything else → None. Splits on the LAST
    separator: legacy rooms use their display name (which may contain " · ") as the id."""
    if not isinstance(title, str) or not title.startswith(GROUP_TITLE_PREFIX):
        return None
    body = title[len(GROUP_TITLE_PREFIX):]
    room, sep, thread = body.rpartition(GROUP_TITLE_SEPARATOR)
    if not sep:
        room, thread = body, ""
    room, thread = room.strip(), thread.strip()
    if not room:
        return None
    return room, (thread if thread and thread != LEGACY_THREAD else None)


def profile_home(root: Path, bot: Optional[str]) -> Path:
    return root if not bot or bot == "default" else root / "profiles" / bot


def _open_ro(db: Path) -> Optional[sqlite3.Connection]:
    if not db.is_file():
        return None
    try:
        return sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=STATE_DB_TIMEOUT_S)
    except (sqlite3.Error, ValueError):
        logger.debug("bot-activity: cannot open %s", db, exc_info=True)
        return None


def _header_name(content: Any) -> Optional[str]:
    match = GROUP_PROMPT_HEADER.match(content) if isinstance(content, str) else None
    return match.group("name") if match else None


_FIRST_USER_MESSAGE = "SELECT content FROM messages WHERE session_id = ? AND role = 'user' ORDER BY id LIMIT 1"


def _lineage(conn: sqlite3.Connection, session_id: str) -> tuple[Optional[RoomRef], str]:
    """Walk ``session_id`` → its ancestors. Returns the nearest ``Group:`` title found (subagents
    and compression continuations inherit their ancestor's room) and the lineage root id."""
    sid = root = session_id
    for _ in range(MAX_PARENT_DEPTH):
        row = conn.execute("SELECT title, parent_session_id FROM sessions WHERE id = ?", (sid,)).fetchone()
        if row is None:
            break
        ref = parse_group_title(row[0])
        if ref:
            return ref, sid
        root = sid
        if not row[1]:
            break
        sid = row[1]
    return None, root


def _root_header(conn: sqlite3.Connection, session_id: str) -> Optional[str]:
    """The ``[Group chat: "<name>"]`` header of the lineage root's opening prompt. The root, not
    the session itself: a compression continuation opens with a handoff, not the header."""
    sid = session_id
    for _ in range(MAX_PARENT_DEPTH):
        row = conn.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (sid,)).fetchone()
        if row is None or not row[0]:
            break
        sid = row[0]
    first = conn.execute(_FIRST_USER_MESSAGE, (sid,)).fetchone()
    return _header_name(first[0] if first else None)


class SessionRoomResolver:
    """``(bot, session_id)`` → ``(room_id, thread_id)`` from the Bot profiles' ``state.db``.
    Title-derived answers are cached for good (a session never moves rooms); header-derived
    guesses and misses are provisional (see ``PROVISIONAL_TTL_S``)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        # key -> (ref, cached_at, permanent)
        self._cache: "OrderedDict[tuple[str, str], tuple[Optional[RoomRef], float, bool]]" = OrderedDict()
        self._aliases: dict[str, set[str]] = {}
        self._aliases_at = float("-inf")

    def resolve(self, bot: Optional[str], session_id: Optional[str]) -> Optional[RoomRef]:
        if not session_id:
            return None
        key = (bot or "default", session_id)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and (cached[2] or now - cached[1] < PROVISIONAL_TTL_S):
                self._cache.move_to_end(key)
                return cached[0]
        ref, permanent = self._lookup(key[0], session_id)
        with self._lock:
            self._cache[key] = (ref, now, permanent)
            self._cache.move_to_end(key)
            while len(self._cache) > RESOLVE_CACHE_MAX:
                self._cache.popitem(last=False)
        return ref

    def _lookup(self, bot: str, session_id: str) -> tuple[Optional[RoomRef], bool]:
        conn = _open_ro(profile_home(self.root, bot) / "state.db")
        if conn is None:
            return None, False
        try:
            ref, root = _lineage(conn, session_id)
            if ref:
                return ref, True
            first = conn.execute(_FIRST_USER_MESSAGE, (root,)).fetchone()
        except sqlite3.Error:
            logger.debug("bot-activity: session lookup failed for %s/%s", bot, session_id, exc_info=True)
            return None, False
        finally:
            conn.close()
        name = _header_name(first[0] if first else None)
        return (self._room_by_name(name) if name else None), False

    def _room_by_name(self, name: str) -> Optional[RoomRef]:
        """The room a session's header names — never its thread (only a title says which thread;
        a sibling's thread would be a guess). A LIVE room with that name wins: Desktop keeps live
        names unique, and a new room may reuse a deleted room's default name. Otherwise the name
        is an old one (the room was renamed): match it to titled sessions that carried it."""
        live = [r["room_id"] for r in group_rooms(self.root) if r["name"] == name]
        if len(live) == 1:
            return live[0], None
        if live:
            return None
        rooms = self._header_aliases().get(name, set())
        return (next(iter(rooms)), None) if len(rooms) == 1 else None

    def _header_aliases(self) -> dict[str, set[str]]:
        """Header room name → room ids, from every profile's ``Group:``-titled sessions. Rebuilt at
        most once per ``ALIAS_TTL_S``."""
        now = time.monotonic()
        with self._lock:
            if now - self._aliases_at < ALIAS_TTL_S:
                return self._aliases
        aliases: dict[str, set[str]] = {}
        profiles_dir = self.root / "profiles"
        homes = [self.root] + (sorted(p for p in profiles_dir.iterdir() if p.is_dir()) if profiles_dir.is_dir() else [])
        for home in homes:
            conn = _open_ro(home / "state.db")
            if conn is None:
                continue
            try:
                for sid, title in conn.execute("SELECT id, title FROM sessions WHERE title LIKE 'Group: %'").fetchall():
                    ref, name = parse_group_title(title), _root_header(conn, sid)
                    if ref and name:
                        aliases.setdefault(name, set()).add(ref[0])
            except sqlite3.Error:
                logger.debug("bot-activity: alias scan failed for %s", home, exc_info=True)
            finally:
                conn.close()
        with self._lock:
            self._aliases, self._aliases_at = aliases, now
        return aliases


_yaml_cache: dict[Path, tuple[float, dict]] = {}
_yaml_lock = threading.Lock()


def _ui_meta(path: Path) -> dict:
    """``ui_meta`` of one ``profile.yaml``, re-parsed only when the file changes."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    with _yaml_lock:
        cached = _yaml_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
    try:
        # Hermes's own YAML loader (ruamel-backed, in core since 0.21.6); PyYAML is not shipped.
        import hermes_yaml

        data = hermes_yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.debug("bot-activity: unreadable %s", path, exc_info=True)
        return {}
    meta = data.get("ui_meta") if isinstance(data, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    with _yaml_lock:
        _yaml_cache[path] = (mtime, meta)
    return meta


def group_rooms(root: Path) -> list[dict[str, Any]]:
    """Rooms from the Desktop's gateway mirror: ``[{room_id, name, members}]``. ``room_id`` falls
    back to the display name for legacy rooms, matching their session titles."""
    groups = _ui_meta(root / "profile.yaml").get("hermes-bots-groups")
    rooms = groups.get("rooms") if isinstance(groups, dict) else None
    out = []
    for room in (rooms.values() if isinstance(rooms, dict) else []):
        if not isinstance(room, dict):
            continue
        name = str(room.get("name") or "").strip()
        room_id = str(room.get("roomId") or name).strip()
        if not room_id:
            continue
        members = [str(m.get("name")) for m in room.get("members") or [] if isinstance(m, dict) and m.get("name")]
        out.append({"room_id": room_id, "name": name or room_id, "members": members,
                    "threads": _thread_topics(room.get("log"))})
    return out


def _thread_topics(log: Any) -> dict[str, str]:
    """Each thread's opening user message, as a human label for thread ids like ``tmvf1tots-7er9r``.
    The mirror is head-trimmed, so an old thread may have no topic."""
    topics: dict[str, str] = {}
    for entry in log if isinstance(log, list) else []:
        if not isinstance(entry, dict):
            continue
        thread, sender = entry.get("thread"), entry.get("from")
        if not thread or thread in topics or not isinstance(sender, dict) or sender.get("kind") != "user":
            continue
        text = " ".join(str(entry.get("text") or "").split())
        if text:
            topics[str(thread)] = text if len(text) <= THREAD_TOPIC_CHARS else text[:THREAD_TOPIC_CHARS] + "…"
    return topics


def bot_title(root: Path, bot: str) -> Optional[str]:
    """The Bot's display title from its profile's ``ui_meta.hermes-bots.title``."""
    bots = _ui_meta(profile_home(root, bot) / "profile.yaml").get("hermes-bots")
    title = bots.get("title") if isinstance(bots, dict) else None
    return str(title).strip() or None if title else None
