"""bot-activity — record what every Bot is doing so Hermes Desktop can show it live.

Observer-only: every hook returns ``None`` and only enqueues an op, so a slow disk can never
delay or veto a turn. Sources merged into one timeline (see ``store.py``):

  pre/post_llm_call             a Bot's turn started / finished (any chat: 1:1, room, bot-to-bot)
  pre/post_tool_call            tool name, args, result, status, duration
  pre/post_approval_request     a Bot is waiting on a human approval, and the answer
  on_room_member_activity       hosted-room members' hidden-session stream (reasoning/message text,
                                errors), plus room coordinates for their tool calls

Desktop Group Chat member sessions are titled ``Group: <roomId> · <threadId>``; the writer maps
each session to its room/thread (``rooms.py``) so the pane can group activity per Group Chat.

Enable the plugin in EVERY Bot profile (the plugin manager is per profile home). All profiles
write to the same ``<hermes root>/plugin-data/bot-activity/activity.db``.

Settings (``plugins.entries.bot-activity.settings``):
  retention_days    Drop rows not updated for this many days (default 3, 0 = keep).
  max_rows          Keep at most this many rows (default 20000, 0 = unlimited).
  max_field_chars   Cap for args/result/text fields (default 4000).
  include_reasoning Record Group Chat reasoning streams (default true).
"""

from __future__ import annotations

import functools
import json
import logging
import math
import sys
import threading
import time
import uuid
from collections import OrderedDict, deque
from typing import Any, Callable, Hashable, Optional

from .rooms import SessionRoomResolver
from .store import PLUGIN_ID, ActivityWriter, current_writer, default_db_path, shared_writer, turn_row_key

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 3
DEFAULT_MAX_ROWS = 20_000
DEFAULT_MAX_FIELD_CHARS = 4000
TITLE_CHARS = 160
STREAM_TAIL_CHARS = 12_000
JSON_MAX_DEPTH = 5
JSON_MAX_ITEMS = 40
JSON_MAX_STRING = 2000
# Text is cut to cap + margin BEFORE redaction so a multi-MB write_file arg costs the same as a
# small one; the margin keeps a secret straddling the cap whole until the final clip.
REDACT_MARGIN = 1024
PENDING_MAX = 500
REDACTION_UNAVAILABLE = "[redacted: redaction unavailable]"

_ROOM_STREAM_KINDS = {"reasoning.delta": "reasoning", "message.delta": "message", "message.interim": "message"}


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def _setting(ctx, key: str, default: float, cast: Callable[[Any], Any]) -> Any:
    raw = ctx.get_config(key, default=default)
    try:
        return cast(raw if raw is not None else default)
    except (TypeError, ValueError):
        logger.warning("bot-activity: invalid setting %s=%r, using %r", key, raw, default)
        return cast(default)


def _redact(text: str) -> str:
    """Fail closed: everything recorded here is shared across profiles and broadcast, so a
    broken redactor must hide the text rather than persist it raw."""
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True, redact_url_credentials=True)
    except Exception:
        logger.debug("bot-activity: redaction failed", exc_info=True)
        return REDACTION_UNAVAILABLE


def _clip(text: Any, limit: int) -> Optional[str]:
    if text is None:
        return None
    text = text if isinstance(text, str) else str(text)
    redacted = _redact(text[:limit + REDACT_MARGIN])
    overflow = len(text) - limit
    if overflow <= 0 and len(redacted) <= limit:
        return redacted
    return redacted[:limit] + f"…[+{max(overflow, 0)} chars]"


def _jsonable(value: Any, depth: int = 0) -> Any:
    if depth > JSON_MAX_DEPTH:
        return repr(value)[:200]
    if isinstance(value, str):
        return value[:JSON_MAX_STRING]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in list(value.items())[:JSON_MAX_ITEMS]}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v, depth + 1) for v in list(value)[:JSON_MAX_ITEMS]]
    return repr(value)[:500]


def _json_field(value: Any, limit: int) -> Optional[str]:
    # Empty containers carry no information and must not overwrite real args on merge.
    if value is None or value == {} or value == [] or value == "":
        return None
    if isinstance(value, str):
        # Tool results are usually JSON text already; keep them parseable when they fit.
        return _clip(value, limit)
    return _clip(json.dumps(_jsonable(value), ensure_ascii=False, default=str), limit)


def _ms(value: Any) -> Optional[int]:
    return int(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def _current_bot() -> str:
    """The profile whose plugin manager fired the hook — hooks (and the queued room-activity
    consumer, which copies the turn's context) run under that profile's Hermes home."""
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name()
    except Exception:
        return "default"


def _broadcast(rows: list[dict[str, Any]]) -> None:
    # Only the Desktop backend process has clients to reach; elsewhere (``hermes chat``, cron, the
    # messaging gateway) importing the transport stack would be wasted work. Those writes still
    # land in SQLite and the Desktop pane's poll picks them up.
    if "tui_gateway.server" not in sys.modules:
        return
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event(PLUGIN_ID, "changed", {"rows": rows, "refetch": not rows})
    except Exception:
        logger.debug("bot-activity: broadcast failed", exc_info=True)


def _observer(fn: Callable[..., None]) -> Callable[..., None]:
    """Hooks are observers: never raise into the turn (``pre_tool_call`` is fail-closed — an
    exception there would block the tool) and always return None (no directive)."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        try:
            fn(*args, **kwargs)
        except Exception:
            logger.debug("bot-activity: %s failed", fn.__name__, exc_info=True)
    return wrapper


class _Pending:
    """FIFO keys for start/end pairs that carry no shared id (an approval has no request id;
    a tool/turn id may be missing), so the end event updates the row its start created."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: "OrderedDict[Hashable, deque[str]]" = OrderedDict()

    def push(self, scope: Hashable, prefix: str) -> str:
        key = f"{prefix}:{uuid.uuid4().hex}"
        with self._lock:
            self._queues.setdefault(scope, deque()).append(key)
            self._queues.move_to_end(scope)
            while len(self._queues) > PENDING_MAX:
                self._queues.popitem(last=False)
        return key

    def pop(self, scope: Hashable, prefix: str) -> str:
        with self._lock:
            keys = self._queues.get(scope)
            if keys:
                key = keys.popleft()
                if not keys:
                    del self._queues[scope]
                return key
        return f"{prefix}:{uuid.uuid4().hex}"


def register(ctx) -> None:
    retention_days = _setting(ctx, "retention_days", DEFAULT_RETENTION_DAYS, float)
    max_rows = _setting(ctx, "max_rows", DEFAULT_MAX_ROWS, int)
    cap = _setting(ctx, "max_field_chars", DEFAULT_MAX_FIELD_CHARS, int) or DEFAULT_MAX_FIELD_CHARS
    include_reasoning = _as_bool(ctx.get_config("include_reasoning", default=True), True)

    db_path = default_db_path()
    # db_path is <root>/plugin-data/bot-activity/activity.db; profiles live under <root>.
    resolver = SessionRoomResolver(db_path.parents[2])
    writer = shared_writer(lambda: ActivityWriter(
        db_path, retention_days=retention_days, max_rows=max_rows, redact=_redact,
        resolve_room=resolver.resolve, on_commit=_broadcast))
    pending = _Pending()

    def submit(**op: Any) -> None:
        op.setdefault("bot", _current_bot())
        op["at"] = time.time()
        current_writer(writer).submit(op)

    def turn_key(session_id: Any, turn_id: Any, *, start: bool) -> str:
        if turn_id:
            return turn_row_key(session_id, turn_id)
        scope = ("turn", session_id)
        return pending.push(scope, "turn") if start else pending.pop(scope, "turn")

    def tool_key(session_id: Any, tool_name: Any, tool_call_id: Any, *, start: bool) -> str:
        if tool_call_id:
            return f"tool:{tool_call_id}"
        scope = ("tool", session_id, tool_name)
        return pending.push(scope, "tool") if start else pending.pop(scope, "tool")

    @_observer
    def on_pre_llm_call(session_id=None, turn_id=None, user_message=None, model=None, platform=None,
                        **_kw) -> None:
        submit(key=turn_key(session_id, turn_id, start=True), kind="turn", status="running",
               session_id=session_id, turn_id=turn_id, title=_clip(user_message, TITLE_CHARS) or "(turn)",
               args=_json_field({"model": model, "platform": platform}, cap))

    @_observer
    def on_post_llm_call(session_id=None, turn_id=None, assistant_response=None, **_kw) -> None:
        submit(key=turn_key(session_id, turn_id, start=False), kind="turn", status="done",
               session_id=session_id, turn_id=turn_id, text=_clip(assistant_response, cap), finish=True)

    @_observer
    def on_pre_tool_call(tool_name=None, args=None, session_id=None, turn_id=None, tool_call_id=None,
                         **_kw) -> None:
        submit(key=tool_key(session_id, tool_name, tool_call_id, start=True), kind="tool", status="running",
               session_id=session_id, turn_id=turn_id, title=str(tool_name or "tool"),
               args=_json_field(args, cap))

    @_observer
    def on_post_tool_call(tool_name=None, args=None, result=None, session_id=None, turn_id=None,
                          tool_call_id=None, duration_ms=None, status=None, error_message=None,
                          **_kw) -> None:
        failed = status not in (None, "ok", "success")
        submit(key=tool_key(session_id, tool_name, tool_call_id, start=False), kind="tool",
               status="error" if failed else "ok", session_id=session_id, turn_id=turn_id,
               title=str(tool_name or "tool"), args=_json_field(args, cap),
               result=_json_field(error_message if failed and error_message else result, cap),
               duration_ms=_ms(duration_ms), finish=True)

    # Approvals carry no request id across pre/post; pair them FIFO per (session, command) so a
    # repeated command gets its own row and its own timing.
    @_observer
    def on_pre_approval_request(command=None, description=None, session_key=None, surface=None,
                                **_kw) -> None:
        submit(key=pending.push(("approval", session_key, command), "approval"), kind="approval",
               status="waiting", title=_clip(description or "Approval requested", TITLE_CHARS),
               text=_clip(command, cap), args=_json_field({"surface": surface, "session_key": session_key}, cap))

    @_observer
    def on_post_approval_response(command=None, session_key=None, choice=None, **_kw) -> None:
        # choice: once | session | always | deny | timeout | cancelled (tools/approval_prompt.py)
        choice_text = str(choice or "cancelled").lower()
        status = "approved" if choice_text in ("once", "session", "always") else choice_text
        submit(key=pending.pop(("approval", session_key, command), "approval"), kind="approval",
               status=status, result=_json_field({"choice": choice}, cap), finish=True)

    @_observer
    def on_room_member_activity(room_id=None, thread_id=None, member_id=None, turn_id=None, task_id=None,
                                kind=None, payload=None, **_kw) -> None:
        payload = payload if isinstance(payload, dict) else {}
        room = {"room_id": room_id, "thread_id": thread_id, "member_id": member_id}
        if kind in _ROOM_STREAM_KINDS:
            stream = _ROOM_STREAM_KINDS[kind]
            if stream == "reasoning" and not include_reasoning:
                return
            text = str(payload.get("text") or payload.get("delta") or "")
            if kind == "message.interim":
                # A sealed segment; when already_streamed its text arrived as message.delta.
                if payload.get("already_streamed"):
                    return
                text = f"\n{text}"
            if text.strip():
                # Redacted by the writer on the ACCUMULATED text (a secret split across deltas
                # only matches once joined), so it is appended raw here.
                submit(key=f"stream:{room_id}:{turn_id or task_id or '-'}:{stream}", kind=stream,
                       status="streaming", turn_id=turn_id, title=stream, text=text, append_text=True,
                       text_cap=STREAM_TAIL_CHARS, **room)
        # Room tool frames share their key with the tool hooks (tool_id == tool_call_id). The hooks are
        # authoritative (full args/result, agent turn id), so these only tag the row with the room and
        # fill gaps. turn_id is omitted: the room's turn id differs from the agent's and would split
        # the call away from its turn header.
        elif kind == "tool.started":
            tool_id = payload.get("tool_id") or uuid.uuid4().hex
            submit(key=f"tool:{tool_id}", kind="tool", status="running", fill_only=True,
                   title=str(payload.get("name") or "tool"), args=_json_field(payload.get("args"), cap), **room)
        elif kind == "tool.completed":
            tool_id = payload.get("tool_id") or uuid.uuid4().hex
            result = payload.get("result") or payload.get("summary") or payload.get("result_text")
            failed = isinstance(result, dict) and bool(result.get("error"))
            duration_s = payload.get("duration_s")
            submit(key=f"tool:{tool_id}", kind="tool", status="error" if failed else "ok", fill_only=True,
                   title=str(payload.get("name") or "tool"), result=_json_field(result, cap),
                   duration_ms=_ms(duration_s * 1000) if isinstance(duration_s, (int, float)) else None,
                   finish=True, **room)
        elif kind == "turn.error":
            submit(key=f"error:{uuid.uuid4().hex}", kind="error", status="error", turn_id=turn_id,
                   title=_clip(payload.get("message") or payload.get("error") or "Turn failed", TITLE_CHARS),
                   text=_json_field(payload, cap), **room)
        elif kind == "tool.output_risk":
            submit(key=f"risk:{uuid.uuid4().hex}", kind="warning", status="warn", turn_id=turn_id,
                   title=_clip(payload.get("message") or "Tool output flagged", TITLE_CHARS),
                   text=_json_field(payload, cap), **room)
        # request.opened (room approvals) is deliberately not recorded: the gateway approval path
        # already fires pre/post_approval_request, which give the row a resolution.

    for hook, fn in (
        ("pre_llm_call", on_pre_llm_call),
        ("post_llm_call", on_post_llm_call),
        ("pre_tool_call", on_pre_tool_call),
        ("post_tool_call", on_post_tool_call),
        ("pre_approval_request", on_pre_approval_request),
        ("post_approval_response", on_post_approval_response),
        ("on_room_member_activity", on_room_member_activity),
    ):
        ctx.register_hook(hook, fn)
    logger.info("bot-activity: recording to %s", writer.path)
