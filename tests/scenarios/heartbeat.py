"""A long turn stays 'working' while tool calls inside it keep arriving."""
import importlib.util, os, sqlite3, sys, tempfile, time
from pathlib import Path
from unittest import mock

home = Path(tempfile.mkdtemp(prefix="hermes-hb-")); os.environ["HERMES_HOME"] = str(home)
PLUGIN = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("bot_activity_pkg", PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)])
pkg = importlib.util.module_from_spec(spec); sys.modules["bot_activity_pkg"] = pkg; spec.loader.exec_module(pkg)
store = sys.modules["bot_activity_pkg.store"]
class Ctx:
    hooks = {}
    def get_config(self, k, default=None): return default
    def register_hook(self, n, f): self.hooks[n] = f
ctx = Ctx(); pkg.register(ctx); h = ctx.hooks
api_spec = importlib.util.spec_from_file_location("api", PLUGIN / "dashboard/plugin_api.py")
api = importlib.util.module_from_spec(api_spec); api_spec.loader.exec_module(api)

started = time.time() - 47 * 60
with mock.patch.object(pkg.time, "time", return_value=started):
    h["pre_llm_call"](session_id="s", turn_id="T", user_message="long build")
h["pre_tool_call"](tool_name="other", args={}, session_id="s2", turn_id="X", tool_call_id="unrelated")  # other turn: no effect
time.sleep(0.8)
active_keys = lambda: [a["key"] for b in api.bots()["bots"] for a in b["active"]]
assert "turn:s:T" not in active_keys(), "47-min-old running turn with no activity is stale"

h["pre_tool_call"](tool_name="terminal", args={"command": "./gradlew test"}, session_id="s", turn_id="T", tool_call_id="c1")
h["post_tool_call"](tool_name="terminal", args={}, result="ok", session_id="s", turn_id="T", tool_call_id="c1", duration_ms=900, status="ok")
time.sleep(0.8)
conn = sqlite3.connect(store.default_db_path())
ts, updated, status = conn.execute("SELECT ts, updated, status FROM events WHERE key = 'turn:s:T'").fetchone()
assert status == "running" and abs(ts - started) < 1 and time.time() - updated < 5, (ts, updated)
active = [a for b in api.bots()["bots"] for a in b["active"]]
assert "turn:s:T" in [a["key"] for a in active] and all("updated" in a for a in active), active
revs = [r for (r,) in conn.execute("SELECT rev FROM events")]; assert len(revs) == len(set(revs)), revs

h["post_llm_call"](session_id="s", turn_id="T", assistant_response="done")
time.sleep(0.8)
h["pre_tool_call"](tool_name="late", args={}, session_id="s", turn_id="T", tool_call_id="c2")  # after the turn ended
time.sleep(0.8)
assert conn.execute("SELECT status FROM events WHERE key = 'turn:s:T'").fetchone()[0] == "done", "heartbeat must not revive a finished turn"
print("HEARTBEAT PASSED")
