"""Group Chat attribution: resolver, live annotation, backfill, /rooms and /bots titles."""
import importlib.util, os, sqlite3, sys, tempfile, time
from pathlib import Path

home = Path(tempfile.mkdtemp(prefix="hermes-rooms-")); os.environ["HERMES_HOME"] = str(home)
PLUGIN = Path(__file__).resolve().parents[2]
(home / "profiles/droid").mkdir(parents=True)
(home / "profiles/droid/config.yaml").write_text("{}\n")
for prof in (home, home / "profiles/droid"):
    db = sqlite3.connect(prof / "state.db")
    db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, parent_session_id TEXT)")
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT)")
    db.executemany("INSERT INTO sessions VALUES (?, ?, ?)", [
        ("s-roomA-t1", "Group: rmA · tmA1", None), ("s-roomA-t2", "Group: rmA · tmA2", None),
        ("s-roomB", "Group: rmB · tmB1", None), ("s-direct", "Bot Chat", None), ("s-legacy", "Group: Old Team · legacy", None),
        # re-titled member session + its subagent: resolved via the opening header of a titled sibling
        ("s-retitled", "Implement the epic", None), ("s-sub", "Subagent: task T-04", "s-retitled")])
    db.executemany("INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)", [
        ("s-roomB", "user", '[Group chat: "Review Room (old name)"] You are @x'),
        ("s-retitled", "user", '[Group chat: "Review Room (old name)"] You are @droid'),
        ("s-direct", "user", "hello")])
    db.commit(); db.close()
(home / "profile.yaml").write_text("""
ui_meta:
  hermes-bots: {title: Principal Engineer}
  hermes-bots-groups:
    version: 3
    rooms:
      id:rmA:
        name: Android Squad
        roomId: rmA
        members: [{name: default}, {name: droid}]
        log:
          - {thread: tmA1, from: {kind: user, name: You}, text: "Build the   release APK please"}
          - {thread: tmA1, from: {kind: bot, name: droid}, text: ok}
          - {thread: tmA2, from: {kind: user, name: You}, text: Second topic}
      id:rmB: {name: Review Room, roomId: rmB, members: [{name: droid}], log: []}
""")
(home / "profiles/droid/profile.yaml").write_text("ui_meta:\n  hermes-bots: {title: Android Implementer}\n")

spec = importlib.util.spec_from_file_location("bot_activity_pkg", PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)])
pkg = importlib.util.module_from_spec(spec); sys.modules["bot_activity_pkg"] = pkg
store = None
# Pre-existing unattributed row (as recorded by v0.1) to exercise the backfill.
spec_store = importlib.util.spec_from_file_location("bot_activity_pkg.store", PLUGIN / "store.py")
store = importlib.util.module_from_spec(spec_store); sys.modules["bot_activity_pkg.store"] = store; spec_store.loader.exec_module(store)
pre = store.connect(store.default_db_path())
pre.execute("INSERT INTO events (key, rev, ts, updated, bot, session_id, kind, status, title) VALUES ('old', 1, ?, ?, 'droid', 's-roomB', 'tool', 'ok', 'read_file')", (time.time(), time.time()))
pre.close()
spec.loader.exec_module(pkg)

class Ctx:
    hooks = {}
    def get_config(self, k, default=None): return default
    def register_hook(self, n, f): self.hooks[n] = f
ctx = Ctx(); pkg.register(ctx); h = ctx.hooks
from unittest import mock
with mock.patch.object(pkg, "_current_bot", return_value="droid"):
    for sid, tc in (("s-roomA-t1", "a1"), ("s-roomA-t2", "a2"), ("s-roomB", "b1"), ("s-direct", "d1"), ("s-legacy", "l1"), ("s-sub", "sub1")):
        h["pre_llm_call"](session_id=sid, turn_id=f"turn-{tc}", user_message="go")
        h["pre_tool_call"](tool_name="terminal", args={"command": "ls"}, session_id=sid, turn_id=f"turn-{tc}", tool_call_id=tc)
time.sleep(1.2)

conn = sqlite3.connect(store.default_db_path()); conn.row_factory = sqlite3.Row
got = {r["key"]: (r["room_id"], r["thread_id"]) for r in conn.execute("SELECT key, room_id, thread_id FROM events")}
print(got)
assert got["tool:a1"] == ("rmA", "tmA1") and got["tool:a2"] == ("rmA", "tmA2") and got["turn:s-roomA-t1:turn-a1"] == ("rmA", "tmA1")
assert got["tool:b1"] == ("rmB", "tmB1") and got["tool:d1"] == (None, None) and got["tool:l1"] == ("Old Team", None)
assert got["old"] == ("rmB", "tmB1"), "backfill"
assert got["tool:sub1"] == ("rmB", None), got["tool:sub1"]  # subagent -> re-titled parent -> header alias (room only)

api_spec = importlib.util.spec_from_file_location("bot_activity_api", PLUGIN / "dashboard/plugin_api.py")
api = importlib.util.module_from_spec(api_spec); api_spec.loader.exec_module(api)
rooms = {r["room_id"]: r for r in api.group_rooms()["rooms"]}
print({k: (v["name"], v["active"], v["deleted"], v["threads"]) for k, v in rooms.items()})
assert rooms["rmA"]["name"] == "Android Squad" and rooms["rmA"]["active"] == 4 and rooms["rmB"]["active"] == 4 and rooms["rmA"]["members"] == ["default", "droid"]
assert rooms["rmA"]["threads"] == {"tmA1": "Build the release APK please", "tmA2": "Second topic"}
assert rooms["Old Team"]["deleted"] and rooms["Old Team"]["name"] is None
assert list(rooms)[0] in ("rmA", "rmB", "Old Team")  # active rooms sort first
titles = {b["bot"]: b["title"] for b in api.bots()["bots"]}; print(titles)
assert titles == {"default": "Principal Engineer", "droid": "Android Implementer"}
assert api.activity(since=0, bot=None, room="rmA", limit=50)["rows"] and all(r["room_id"] == "rmA" for r in api.activity(since=0, bot=None, room="rmA", limit=50)["rows"])

# ── review findings ──
rooms_mod = sys.modules["bot_activity_pkg.rooms"]
assert rooms_mod.parse_group_title("Group: R&D · Ops · legacy") == ("R&D · Ops", None)
assert rooms_mod.parse_group_title("Group: rmX") == ("rmX", None)
assert rooms_mod._header_name('[Group chat: "Fix "login" bug"] You are @a') == 'Fix "login" bug'

droid_db = sqlite3.connect(home / "profiles/droid/state.db")
def add_session(sid, title, header):
    droid_db.execute("INSERT INTO sessions VALUES (?, ?, NULL)", (sid, title))
    droid_db.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'user', ?)", (sid, f'[Group chat: "{header}"] You are @droid'))
    droid_db.commit()
# 1) First turn of a NEW thread in rmA: no title yet -> provisional room only; title lands -> thread fixed.
add_session("s-new-thread", None, "Android Squad")
res = rooms_mod.SessionRoomResolver(home)
assert res.resolve("droid", "s-new-thread") == ("rmA", None)
droid_db.execute("UPDATE sessions SET title = 'Group: rmA · tmNEW' WHERE id = 's-new-thread'"); droid_db.commit()
assert res.resolve("droid", "s-new-thread") == ("rmA", None)  # still provisional within TTL
with mock.patch.object(rooms_mod, "PROVISIONAL_TTL_S", 0):
    assert res.resolve("droid", "s-new-thread") == ("rmA", "tmNEW")
# 2) A live room reusing a deleted room's old name wins over titled sessions of the deleted room.
add_session("s-deleted-room", "Group: rmDEAD · tmD", "Review Room")   # deleted room once had the name
add_session("s-reused", None, "Review Room")                           # live rmB is named "Review Room" now
assert rooms_mod.SessionRoomResolver(home).resolve("droid", "s-reused") == ("rmB", None)

# 3) Repair pass fixes first-turn rows once the title lands; a raising resolver never drops a batch.
with mock.patch.object(pkg, "_current_bot", return_value="droid"):
    add_session("s-late", None, "Android Squad")
    h["pre_tool_call"](tool_name="t", args={"a": 1}, session_id="s-late", turn_id="tl", tool_call_id="late1")
    time.sleep(0.8)
    assert conn.execute("SELECT room_id, thread_id FROM events WHERE key='tool:late1'").fetchone()[:] == ("rmA", None)
    droid_db.execute("UPDATE sessions SET title = 'Group: rmA · tmLATE' WHERE id = 's-late'"); droid_db.commit()
    writer = store.shared_writer(lambda: None)
    with mock.patch.object(rooms_mod, "PROVISIONAL_TTL_S", 0), mock.patch.object(store, "REPAIR_EVERY_S", 0):
        h["pre_tool_call"](tool_name="t", args={}, session_id="s-direct", turn_id="x", tool_call_id="trigger")
        time.sleep(0.8)
    assert conn.execute("SELECT room_id, thread_id FROM events WHERE key='tool:late1'").fetchone()[:] == ("rmA", "tmLATE")
    original = writer.resolve_room
    writer.resolve_room = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
    h["pre_tool_call"](tool_name="t", args={}, session_id="s-roomA-t1", turn_id="x", tool_call_id="survives")
    time.sleep(0.8)
    writer.resolve_room = original
    assert conn.execute("SELECT status FROM events WHERE key='tool:survives'").fetchone()[0] == "running"

# 4) Prune keeps the most recently UPDATED rows even if older rows hold higher revs.
pc = store.connect(Path(tempfile.mkdtemp()) / "p.db")
for i in range(5):
    pc.execute("INSERT INTO events (key, rev, ts, updated, kind) VALUES (?, ?, ?, ?, 'tool')", (f"new{i}", i, 100 + i, 100 + i))
    pc.execute("INSERT INTO events (key, rev, ts, updated, kind) VALUES (?, ?, ?, ?, 'tool')", (f"old{i}", 100 + i, i, i))
store.prune(pc, 0, 5)
assert sorted(k for (k,) in pc.execute("SELECT key FROM events")) == [f"new{i}" for i in range(5)]
print("ROOM CHECKS PASSED")
