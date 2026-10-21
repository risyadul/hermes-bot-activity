"""Exercise bot-activity hooks + backend against a throwaway HERMES_HOME."""
import importlib.util, json, os, sys, tempfile, time

# Assembled at runtime so the repository never contains a string shaped like a real key.
FAKE_KEY = "sk-ant-" + "api03-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"
from pathlib import Path

home = Path(tempfile.mkdtemp(prefix="hermes-test-"))
os.environ["HERMES_HOME"] = str(home)
PLUGIN = Path(__file__).resolve().parents[2]

spec = importlib.util.spec_from_file_location("bot_activity_pkg", PLUGIN / "__init__.py",
                                              submodule_search_locations=[str(PLUGIN)])
pkg = importlib.util.module_from_spec(spec); sys.modules["bot_activity_pkg"] = pkg; spec.loader.exec_module(pkg)

class Ctx:
    hooks = {}
    def get_config(self, key, default=None): return default
    def register_hook(self, name, fn): self.hooks[name] = fn
ctx = Ctx(); pkg.register(ctx)
h = ctx.hooks
assert set(h) == {"pre_llm_call","post_llm_call","pre_tool_call","post_tool_call","pre_approval_request",
                  "post_approval_response","on_room_member_activity"}, h.keys()

h["pre_llm_call"](session_id="s1", turn_id="t1", user_message="build the APK please", model="m", platform="desktop")
h["pre_tool_call"](tool_name="terminal", args={"command": "./gradlew assembleDebug", "api_key": FAKE_KEY},
                   session_id="s1", turn_id="t1", tool_call_id="call_1")
h["on_room_member_activity"](room_id="room9", member_id="android-implementer", turn_id="rt1", kind="tool.started",
                             payload={"tool_id": "call_1", "name": "terminal", "args": {"command": "./gradlew assembleDebug"}})
for piece in ["Let me ", "check the ", "build output."]:
    h["on_room_member_activity"](room_id="room9", member_id="android-implementer", turn_id="rt1", kind="reasoning.delta", payload={"text": piece})
time.sleep(0.2)
h["post_tool_call"](tool_name="terminal", args={}, result='{"output": "BUILD SUCCESSFUL"}', session_id="s1", turn_id="t1",
                    tool_call_id="call_1", duration_ms=4210, status="ok")
h["on_room_member_activity"](room_id="room9", member_id="android-implementer", turn_id="rt1", kind="tool.completed",
                             payload={"tool_id": "call_1", "name": "terminal", "duration_s": 4.2, "summary": "ok"})
h["pre_tool_call"](tool_name="read_file", args={"path": "/x"}, session_id="s1", turn_id="t1", tool_call_id="call_2")
h["post_tool_call"](tool_name="read_file", args={"path": "/x"}, result='{"error":"nope"}', session_id="s1", turn_id="t1",
                    tool_call_id="call_2", duration_ms=12, status="error", error_message="nope")
# room frames arrive BEFORE the tool hooks for call_3; call_4 has only room frames
h["on_room_member_activity"](room_id="room9", member_id="m", turn_id="rt1", kind="tool.started", payload={"tool_id": "call_3", "name": "read_file", "args": {}})
h["on_room_member_activity"](room_id="room9", member_id="m", turn_id="rt1", kind="tool.completed", payload={"tool_id": "call_3", "name": "read_file", "summary": "short"})
time.sleep(0.6)
h["pre_tool_call"](tool_name="read_file", args={"path": "/y"}, session_id="s1", turn_id="t1", tool_call_id="call_3")
h["post_tool_call"](tool_name="read_file", args={"path": "/y"}, result="x", session_id="s1", turn_id="t1", tool_call_id="call_3", duration_ms=5, status="ok")
h["on_room_member_activity"](room_id="room9", member_id="m", turn_id="rt1", kind="tool.started", payload={"tool_id": "call_4", "name": "web_search", "args": {"query": "q"}})
h["on_room_member_activity"](room_id="room9", member_id="m", turn_id="rt1", kind="tool.completed", payload={"tool_id": "call_4", "name": "web_search", "duration_s": 0.8})
h["pre_approval_request"](command="rm -rf build", description="Dangerous delete", session_key="sk1", surface="gateway")
h["post_approval_response"](command="rm -rf build", session_key="sk1", choice="once")
h["on_room_member_activity"](room_id="room9", member_id="android-implementer", turn_id="rt1", kind="turn.error",
                             payload={"message": "provider_quota_limit"})
h["post_llm_call"](session_id="s1", turn_id="t1", assistant_response="APK built at app/build/outputs.")
time.sleep(1.5)

api_spec = importlib.util.spec_from_file_location("bot_activity_api", PLUGIN / "dashboard/plugin_api.py")
api = importlib.util.module_from_spec(api_spec); api_spec.loader.exec_module(api)
res = api.activity(since=0, bot=None, room=None, limit=300)
rows = {r["key"]: r for r in res["rows"]}
for k, r in rows.items():
    print(k, "|", r["bot"], r["kind"], r["status"], r["title"], "| room:", r["room_id"], "| dur:", r["duration_ms"],
          "| text:", (r["text"] or "")[:60])
t1 = rows["tool:call_1"]
assert t1["status"] == "ok" and t1["room_id"] == "room9" and t1["session_id"] == "s1" and t1["duration_ms"] == 4210 and t1["turn_id"] == "t1" and t1["result"] == {"output": "BUILD SUCCESSFUL"}, t1
assert t1["args"]["command"] == "./gradlew assembleDebug", t1["args"]
r3 = rows["tool:call_3"]; assert r3["status"] == "ok" and r3["turn_id"] == "t1" and r3["args"] == {"path": "/y"} and r3["room_id"] == "room9", r3
r4 = rows["tool:call_4"]; assert r4["status"] == "ok" and r4["duration_ms"] == 800, r4
assert FAKE_KEY[:24] not in json.dumps(t1["args"]), t1["args"]
assert rows["tool:call_2"]["status"] == "error" and rows["tool:call_2"]["result"] == "nope"
assert rows["stream:room9:rt1:reasoning"]["text"] == "Let me check the build output."
turn = rows["turn:s1:t1"]; assert turn["status"] == "done" and turn["duration_ms"] is not None and turn["text"].startswith("APK")
assert [r for r in rows.values() if r["kind"] == "approval"][0]["status"] == "approved"
assert any(r["kind"] == "error" for r in rows.values())
assert len(rows) == 8, len(rows)
inc = api.activity(since=res["rev"], bot=None, room=None, limit=300); assert inc["rows"] == [], inc
bots = api.bots(); print("bots:", [(b["bot"], len(b["active"])) for b in bots["bots"]])
print("cleared:", api.clear({})["deleted"]); assert api.activity(since=0, bot=None, room=None, limit=10)["rows"] == []
# Turn titles: multimodal parts -> text only; Group Chat prompt -> newest user line.
tt = pkg._turn_title
assert tt([{"type": "text", "text": "fix the login bug"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 100000}}]) == "fix the login bug"
room_prompt = ('[Group chat: "Demo Room"] You are @demo, one participant in a group chat with the user.\n\n'
               "New messages in the room since your last turn (oldest first):\n"
               "You (user): first ask\nPlanner: I will look\nYou (user): list the files in /tmp please")
assert tt(room_prompt) == "list the files in /tmp please", tt(room_prompt)
bot_only = room_prompt.split("New messages")[0] + "New messages in the room since your last turn (oldest first):\nPlanner: over to you"
assert tt(bot_only) == "Planner: over to you", tt(bot_only)
assert tt("plain 1:1 message") == "plain 1:1 message" and tt(None) is None
print("ALL CHECKS PASSED  (db:", pkg.store.default_db_path() if hasattr(pkg, "store") else home, ")")
