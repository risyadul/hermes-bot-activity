"""bot-activity storage — one SQLite file shared by every profile and process.

Rows are upserted by ``key`` so the two views of one tool call (the per-profile
``pre/post_tool_call`` hooks and the Group Chat ``on_room_member_activity`` hook)
merge into a single timeline entry. Every write bumps ``rev``, a sequence allocated under
BEGIN IMMEDIATE, so readers can poll ``rev > since`` across processes.

Hooks never touch SQLite directly: they enqueue ops on a bounded queue drained by
one daemon writer thread per process, which batches writes and then pushes the
changed rows to connected Desktop windows.
"""

from __future__ import annotations

import atexit
import hashlib
import itertools
import json
import logging
import queue
import sqlite3
import sys
import threading
import time
import types
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

PLUGIN_ID = "bot-activity"
DB_FILENAME = "activity.db"

QUEUE_MAX = 5000
BATCH_MAX = 200
BATCH_WINDOW_S = 0.35
PRUNE_EVERY_S = 3600
BROADCAST_ROWS_MAX = 60
BUSY_TIMEOUT_MS = 5000
WRITE_ATTEMPTS = 3
WRITE_RETRY_BACKOFF_S = 0.5
ERROR_LOG_INTERVAL_S = 60
EXIT_FLUSH_TIMEOUT_S = 2.0
# Revs above this are v0.1.0 nanosecond timestamps (see _next_rev).
LEGACY_REV_THRESHOLD = 10**15
RAW_STREAMS_MAX = 256
REPAIR_EVERY_S = 120
REPAIR_WINDOW_S = 24 * 3600
REPAIR_MAX_SESSIONS = 200
STREAM_REDACT_MARGIN = 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    key          TEXT PRIMARY KEY,
    rev          INTEGER NOT NULL,
    ts           REAL NOT NULL,
    updated      REAL NOT NULL,
    bot          TEXT,
    session_id   TEXT,
    turn_id      TEXT,
    room_id      TEXT,
    thread_id    TEXT,
    member_id    TEXT,
    kind         TEXT NOT NULL,
    status       TEXT,
    title        TEXT,
    args         TEXT,
    result       TEXT,
    text         TEXT,
    duration_ms  INTEGER
);
CREATE INDEX IF NOT EXISTS events_rev ON events(rev);
CREATE INDEX IF NOT EXISTS events_bot_rev ON events(bot, rev);
"""
# Columns added after v0.1.0, applied to existing databases by connect().
_ADDED_COLUMNS = {"thread_id": "TEXT"}
_POST_MIGRATION_INDEXES = "CREATE INDEX IF NOT EXISTS events_room_rev ON events(room_id, rev);"

COLUMNS = ("key", "rev", "ts", "updated", "bot", "session_id", "turn_id", "room_id", "thread_id", "member_id",
           "kind", "status", "title", "args", "result", "text", "duration_ms")

# Columns where a later non-null value replaces the stored one.
_MERGE_COLUMNS = ("bot", "session_id", "turn_id", "room_id", "thread_id", "member_id", "status", "title",
                  "args", "result", "duration_ms")


def default_db_path() -> Path:
    """``<hermes root>/plugin-data/bot-activity/activity.db`` — the ROOT, not the profile home,
    so every Bot's profile writes into the same file."""
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "plugin-data" / PLUGIN_ID / DB_FILENAME


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Autocommit mode: the writer opens its own BEGIN IMMEDIATE; readers never hold a txn open.
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        logger.debug("bot-activity: WAL unavailable for %s", path)
    conn.executescript(SCHEMA)
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
    for column, decl in _ADDED_COLUMNS.items():
        if column not in existing:
            try:
                conn.execute(f"ALTER TABLE events ADD COLUMN {column} {decl}")
            except sqlite3.OperationalError:
                # Fine if another process migrated first; anything else (e.g. locked) must not
                # hand out a connection whose schema lacks the column.
                if column not in {row["name"] for row in conn.execute("PRAGMA table_info(events)")}:
                    raise
    conn.executescript(_POST_MIGRATION_INDEXES)
    return conn


def _merge_sql(fill_only: bool) -> str:
    # Normal ops: a later non-null value wins. ``fill_only`` ops (secondary sources, e.g. the
    # Group Chat view of a tool call the tool hooks already describe) only fill empty columns.
    # Status is the exception: an in-progress status may still advance (a room "completed" frame
    # must finish a row whose post_tool_call never fired), but a settled one is never overwritten.
    pick = "COALESCE(events.{c}, excluded.{c})" if fill_only else "COALESCE(excluded.{c}, events.{c})"
    status_pick = ("CASE WHEN events.status IS NULL OR events.status IN ('running', 'streaming') "
                   "THEN COALESCE(excluded.status, events.status) ELSE events.status END")
    return ", ".join(f"{c} = {status_pick if fill_only and c == 'status' else pick.format(c=c)}"
                     for c in _MERGE_COLUMNS)


def _upsert(conn: sqlite3.Connection, op: dict[str, Any], now: float, rev: int,
            stream_text: Callable[[str, str, str, int], str]) -> str:
    """Apply one op; returns its key. ``at`` is when the hook fired (not when the batch was
    written), ``append_text`` concatenates (tail-capped) instead of replacing, and ``finish``
    computes ``duration_ms`` from the row's first-seen ``ts``."""
    key = op["key"]
    at = op.get("at") or now
    fill_only = bool(op.get("fill_only"))
    conn.execute(
        f"""INSERT INTO events (key, rev, ts, updated, kind, text, {", ".join(_MERGE_COLUMNS)})
            VALUES (?, ?, ?, ?, ?, ?, {", ".join("?" for _ in _MERGE_COLUMNS)})
            ON CONFLICT(key) DO UPDATE SET
              rev = excluded.rev,
              ts = MIN(events.ts, excluded.ts),
              updated = MAX(events.updated, excluded.updated),
              {_merge_sql(fill_only)}""",
        (key, rev, at, op.get("at_last") or at, op["kind"], None, *(op.get(c) for c in _MERGE_COLUMNS)),
    )
    text = op.get("text")
    if text is not None:
        if op.get("append_text"):
            stored = conn.execute("SELECT text FROM events WHERE key = ?", (key,)).fetchone()[0] or ""
            conn.execute("UPDATE events SET text = ? WHERE key = ?",
                         (stream_text(key, text, stored, int(op.get("text_cap") or 0)), key))
        else:
            conn.execute("UPDATE events SET text = ? WHERE key = ?", (text, key))
    if op.get("finish") and op.get("duration_ms") is None:
        # A secondary source must not recompute a duration the primary already recorded.
        guard = " AND duration_ms IS NULL" if fill_only else ""
        conn.execute(f"UPDATE events SET duration_ms = CAST((updated - ts) * 1000 AS INTEGER) WHERE key = ?{guard}",
                     (key,))
    return key


def turn_row_key(session_id: Any, turn_id: Any) -> str:
    return f"turn:{session_id or '?'}:{turn_id}"


def _heartbeat_turn(conn: sqlite3.Connection, op: dict[str, Any], now: float, rev: int) -> Optional[str]:
    """Activity inside a turn keeps that turn's row fresh. A long turn (a 45-minute build loop)
    otherwise has a running row untouched since it started and reads as stale — the Bot looks
    idle between its seconds-long tool calls. Returns the turn's key when it was touched."""
    if op["kind"] == "turn" or not op.get("session_id") or not op.get("turn_id"):
        return None
    key = turn_row_key(op["session_id"], op["turn_id"])
    cur = conn.execute("UPDATE events SET updated = MAX(updated, ?), rev = ? WHERE key = ? AND status = 'running'",
                       (op.get("at_last") or op.get("at") or now, rev, key))
    return key if cur.rowcount else None


def _coalesce_appends(ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive streaming appends for the same key so a burst of deltas is one write."""
    out: list[dict[str, Any]] = []
    for op in ops:
        prev = out[-1] if out else None
        if prev and op.get("append_text") and prev.get("append_text") and prev["key"] == op["key"]:
            out[-1] = {**prev, **{k: v for k, v in op.items() if v is not None},
                       "at": prev.get("at"), "at_last": op.get("at"),
                       "text": (prev.get("text") or "") + (op.get("text") or "")}
        else:
            out.append(op)
    return out


def _next_rev(conn: sqlite3.Connection) -> int:
    """Next value of the cross-process sequence. Called inside BEGIN IMMEDIATE, so it is
    commit-ordered (no writer can commit a lower rev after a reader saw a higher one) and
    independent of wall-clock steps. Small integers also stay exact in JavaScript."""
    current = conn.execute("SELECT COALESCE(MAX(rev), 0) FROM events").fetchone()[0]
    if current > LEGACY_REV_THRESHOLD:
        # v0.1.0 used time.time_ns(); renumber those rows into the sequence once.
        conn.execute("""UPDATE events SET rev = ranked.n
                        FROM (SELECT key, ROW_NUMBER() OVER (ORDER BY rev) AS n FROM events) AS ranked
                        WHERE events.key = ranked.key""")
        current = conn.execute("SELECT COALESCE(MAX(rev), 0) FROM events").fetchone()[0]
    return current + 1


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = {col: row[col] for col in COLUMNS}
    for col in ("args", "result"):
        value = out.get(col)
        if isinstance(value, str) and value[:1] in "{[":
            try:
                out[col] = json.loads(value)
            except ValueError:
                pass
    return out


def fetch_rows(conn: sqlite3.Connection, keys: Iterable[str]) -> list[dict[str, Any]]:
    keys = list(keys)
    if not keys:
        return []
    marks = ",".join("?" for _ in keys)
    return [row_to_dict(r) for r in conn.execute(f"SELECT * FROM events WHERE key IN ({marks})", keys)]


def prune(conn: sqlite3.Connection, retention_days: float, max_rows: int) -> None:
    # By ``updated``, not ``rev``: a room repair gives old rows fresh revs.
    if retention_days > 0:
        conn.execute("DELETE FROM events WHERE updated < ?", (time.time() - retention_days * 86400,))
    if max_rows > 0:
        conn.execute(
            "DELETE FROM events WHERE key NOT IN (SELECT key FROM events ORDER BY updated DESC LIMIT ?)",
            (max_rows,),
        )


_STOP = object()

# (bot, session_id) -> (room_id, thread_id) or None; see rooms.SessionRoomResolver.
RoomResolver = Callable[[Optional[str], str], Optional[tuple[str, Optional[str]]]]


class ActivityWriter:
    """Process-wide batching writer. ``submit`` never blocks: on overflow the op is dropped,
    so a wedged disk can't stall an agent turn. Pending ops are flushed at interpreter exit so
    a short-lived ``hermes chat -q`` still records its final ``done``."""

    def __init__(self, path: Path, *, retention_days: float, max_rows: int,
                 redact: Callable[[str], str],
                 resolve_room: Optional[RoomResolver] = None,
                 on_commit: Optional[Callable[[list[dict[str, Any]]], None]] = None) -> None:
        self.path = path
        self.retention_days = retention_days
        self.max_rows = max_rows
        self.redact = redact
        self.resolve_room = resolve_room
        self.on_commit = on_commit
        # Unredacted stream tails, in memory only (never on disk). Redaction must run on the RAW
        # accumulated text: a secret split across deltas only matches once joined, and appending
        # raw deltas to already-masked text would leak the rest of the secret after the mask.
        self._raw_streams: "OrderedDict[str, str]" = OrderedDict()
        self.dropped = 0
        self._last_error_log = 0.0
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=QUEUE_MAX)
        self._thread = threading.Thread(target=self._run, name="bot-activity-writer", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    def submit(self, op: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(op)
        except queue.Full:
            self.dropped += 1

    def retire(self) -> None:
        """Stop after draining what is already queued; never blocks the caller."""
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            pass

    def close(self, timeout: float = EXIT_FLUSH_TIMEOUT_S) -> None:
        try:
            self._queue.put(_STOP, timeout=timeout)
        except queue.Full:
            return
        self._thread.join(timeout)

    def _drain(self) -> tuple[list[dict[str, Any]], bool]:
        first = self._queue.get()
        if first is _STOP:
            return [], True
        ops = [first]
        deadline = time.monotonic() + BATCH_WINDOW_S
        while len(ops) < BATCH_MAX:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = self._queue.get(timeout=remaining)
            except queue.Empty:
                break
            if item is _STOP:
                return ops, True
            ops.append(item)
        return ops, False

    def _stream_text(self, key: str, piece: str, stored: str, cap: int) -> str:
        raw = self._raw_streams.pop(key, None)
        # Unknown key (evicted, or another process/restart started this stream): the stored,
        # already-redacted text is the best available base.
        raw = (stored if raw is None else raw) + piece
        if cap:
            raw = raw[-(cap + STREAM_REDACT_MARGIN):]
        self._raw_streams[key] = raw
        while len(self._raw_streams) > RAW_STREAMS_MAX:
            self._raw_streams.popitem(last=False)
        redacted = self.redact(raw)
        return redacted[-cap:] if cap else redacted

    def _resolve(self, bot: Optional[str], session_id: str) -> Optional[tuple[str, Optional[str]]]:
        # Attribution is best-effort: a resolver failure must never cost the batch.
        try:
            return self.resolve_room(bot, session_id) if self.resolve_room else None
        except Exception:
            logger.debug("bot-activity: room resolution failed for %s/%s", bot, session_id, exc_info=True)
            return None

    def _with_room(self, op: dict[str, Any]) -> dict[str, Any]:
        """Tag ops from a Group Chat member session with its room/thread (resolved outside the
        write transaction — it reads the Bot profile's state.db)."""
        if op.get("room_id") or not op.get("session_id"):
            return op
        ref = self._resolve(op.get("bot"), op["session_id"])
        return {**op, "room_id": ref[0], "thread_id": ref[1]} if ref else op

    def _repair_rooms(self, conn: sqlite3.Connection) -> None:
        """Attribute recent rows that have no room/thread yet: rows recorded before attribution
        existed, and rows from a session's first turn (its ``Group:`` title lands only after that
        turn, so they got at most a provisional room). Bounded so it stays cheap per process."""
        if self.resolve_room is None:
            return
        pending = conn.execute(
            """SELECT bot, session_id FROM events
               WHERE session_id IS NOT NULL AND (room_id IS NULL OR thread_id IS NULL) AND updated > ?
               GROUP BY bot, session_id ORDER BY MAX(updated) DESC LIMIT ?""",
            (time.time() - REPAIR_WINDOW_S, REPAIR_MAX_SESSIONS)).fetchall()
        fixes = [(bot, sid, ref) for bot, sid in pending if (ref := self._resolve(bot, sid))]
        if not fixes:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            rev = _next_rev(conn)
            for bot, sid, (room_id, thread_id) in fixes:
                # A title-derived thread also corrects a provisional (header-guessed) room.
                stale = conn.execute(
                    """SELECT key FROM events WHERE session_id = ? AND bot IS ?
                         AND (room_id IS NULL OR (? IS NOT NULL AND (thread_id IS NULL OR room_id != ?)))
                       ORDER BY updated""", (sid, bot, thread_id, room_id)).fetchall()
                for (key,) in stale:
                    conn.execute("UPDATE events SET room_id = ?, thread_id = COALESCE(?, thread_id), rev = ? WHERE key = ?",
                                 (room_id, thread_id, rev, key))
                    rev += 1
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def _write_batch(self, conn: sqlite3.Connection, ops: list[dict[str, Any]], prune_now: bool) -> list[str]:
        now = time.time()
        ops = [self._with_room(op) for op in ops]
        conn.execute("BEGIN IMMEDIATE")
        try:
            revs = itertools.count(_next_rev(conn))
            keys: list[str] = []
            for op in _coalesce_appends(ops):
                keys.append(_upsert(conn, op, now, next(revs), self._stream_text))
                if beat := _heartbeat_turn(conn, op, now, next(revs)):
                    keys.append(beat)
            if prune_now:
                prune(conn, self.retention_days, self.max_rows)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return list(dict.fromkeys(keys))

    def _run(self) -> None:
        conn: Optional[sqlite3.Connection] = None
        last_prune = 0.0
        last_repair = float("-inf")
        stopping = False
        while not stopping:
            ops, stopping = self._drain()
            if not ops:
                continue
            for attempt in range(WRITE_ATTEMPTS):
                try:
                    if conn is None:
                        conn = connect(self.path)
                    prune_now = time.time() - last_prune > PRUNE_EVERY_S
                    keys = self._write_batch(conn, ops, prune_now)
                    if prune_now:
                        last_prune = time.time()
                    if self.on_commit:
                        # Too many rows for one frame → an empty list tells clients to refetch instead.
                        self.on_commit(fetch_rows(conn, keys) if len(keys) <= BROADCAST_ROWS_MAX else [])
                    # After the write, so a short-lived process records its own ops first.
                    if time.monotonic() - last_repair > REPAIR_EVERY_S:
                        last_repair = time.monotonic()
                        try:
                            self._repair_rooms(conn)
                        except Exception:
                            logger.debug("bot-activity: room repair failed", exc_info=True)
                    break
                except Exception:
                    if conn is not None:
                        conn.close()
                    conn = None
                    if attempt + 1 < WRITE_ATTEMPTS and not stopping:
                        time.sleep(WRITE_RETRY_BACKOFF_S * (attempt + 1))
                        continue
                    if time.monotonic() - self._last_error_log > ERROR_LOG_INTERVAL_S:
                        self._last_error_log = time.monotonic()
                        logger.exception("bot-activity: dropped %d op(s) after %d attempts", len(ops), attempt + 1)


_RUNTIME_MODULE = "_bot_activity_runtime"
# Identifies this writer implementation. A live plugin load (e.g. "Track now" enabling a new Bot)
# can import a different copy of this file into a process that already runs a writer.
WRITER_VERSION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def _runtime() -> types.ModuleType:
    return sys.modules.setdefault(_RUNTIME_MODULE, types.ModuleType(_RUNTIME_MODULE))


def shared_writer(factory: Callable[[], ActivityWriter]) -> ActivityWriter:
    """One writer per process even though each profile's plugin manager imports this package
    separately; the instance lives on a tiny synthetic module every copy can see. A copy with a
    different writer implementation replaces it (the old one drains its queue, then stops)."""
    runtime = _runtime()
    lock = runtime.__dict__.setdefault("lock", threading.Lock())
    with lock:
        writer = runtime.__dict__.get("writer")
        if writer is None or runtime.__dict__.get("version") != WRITER_VERSION:
            if writer is not None:
                writer.retire()
            writer = factory()
            runtime.writer, runtime.version = writer, WRITER_VERSION
        return writer


def current_writer(fallback: ActivityWriter) -> ActivityWriter:
    """The process's live writer — hooks registered by an older copy must not keep feeding a
    writer that was retired when a newer copy loaded."""
    return _runtime().__dict__.get("writer") or fallback
