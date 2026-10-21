"""Regression tests for the review findings."""
import importlib.util, os, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path

home = Path(tempfile.mkdtemp(prefix="hermes-reg-")); os.environ["HERMES_HOME"] = str(home)
PLUGIN = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("bot_activity_pkg", PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)])
pkg = importlib.util.module_from_spec(spec); sys.modules["bot_activity_pkg"] = pkg; spec.loader.exec_module(pkg)
store = sys.modules["bot_activity_pkg.store"]
class Ctx:
    hooks = {}
    def get_config(self, key, default=None): return {"max_rows": "not-a-number"}.get(key, default)  # bad setting must not crash
    def register_hook(self, n, f): self.hooks[n] = f
ctx = Ctx(); pkg.register(ctx); h = ctx.hooks
DB = store.default_db_path()

# Legacy nanosecond revs present before the first batch must be renumbered.
store.connect(DB).execute("INSERT INTO events (key, rev, ts, updated, kind) VALUES ('legacy', ?, 1, 1, 'tool')", (time.time_ns(),))

SECRET = "sk-ant-api03-" + "A" * 40
room = dict(room_id="r1", member_id="m", turn_id="rt")
for i in range(0, len(SECRET), 6):
    h["on_room_member_activity"](kind="reasoning.delta", payload={"text": SECRET[i:i+6]}, **room)
    time.sleep(0.05)
h["on_room_member_activity"](kind="message.delta", payload={"text": "Hello world."}, **room)
h["on_room_member_activity"](kind="message.interim", payload={"text": "Hello world.", "already_streamed": True}, **room)
h["pre_tool_call"](tool_name="web", args={"url": "https://api.x.com/v1?access_token=abcdef0123456789abcdef"}, session_id="s", turn_id="t", tool_call_id="c1")
h["post_tool_call"](tool_name="web", args={}, result="ok", session_id="s", turn_id="t", tool_call_id="c1", duration_ms=120, status="ok")
time.sleep(1.0)
h["on_room_member_activity"](kind="tool.completed", payload={"tool_id": "c1", "name": "web"}, **room)  # late, no duration_s
h["pre_tool_call"](tool_name="noid", args={"a": "1"}, session_id="s", turn_id="t", tool_call_id=None)
h["post_tool_call"](tool_name="noid", args={"a": "1"}, result="r", session_id="s", turn_id="t", tool_call_id=None, duration_ms=float("nan"), status="ok")
for _ in range(2):
    h["pre_approval_request"](command="rm x", description="del", session_key="k", surface="gateway")
    h["post_approval_response"](command="rm x", session_key="k", choice="deny")
t0 = time.perf_counter(); h["pre_tool_call"](tool_name="write_file", args={"content": "x" * 5_000_000}, session_id="s", turn_id="t", tool_call_id="big")
hook_s = time.perf_counter() - t0
assert h["pre_tool_call"](tool_name=None, args=object(), session_id=None) is None  # weird input: never raises
time.sleep(1.5)

conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
rows = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM events")}
reasoning = rows["stream:r1:rt:reasoning"]["text"]
assert "A" * 8 not in reasoning, reasoning; print("split secret redacted ->", reasoning)
assert rows["stream:r1:rt:message"]["text"] == "Hello world.", rows["stream:r1:rt:message"]["text"]
assert "abcdef0123456789abcdef" not in rows["tool:c1"]["args"], rows["tool:c1"]["args"]; print("url cred ->", rows["tool:c1"]["args"])
assert rows["tool:c1"]["duration_ms"] == 120, rows["tool:c1"]["duration_ms"]
noid = [r for r in rows.values() if r["title"] == "noid"]; assert len(noid) == 1 and noid[0]["status"] == "ok", noid
approvals = [r for r in rows.values() if r["kind"] == "approval"]; assert len(approvals) == 2 and all(a["status"] == "deny" for a in approvals), approvals
assert hook_s < 0.2, hook_s; print(f"5MB arg hook cost: {hook_s*1000:.1f} ms")
revs = sorted(r["rev"] for r in rows.values()); assert revs[-1] < 10_000 and len(set(revs)) == len(revs), revs
print("revs:", revs[:3], "...", revs[-1])

# atexit flush: a short-lived process's last op must land.
script = f"""
import importlib.util, os, sys
os.environ['HERMES_HOME'] = {str(home)!r}
spec = importlib.util.spec_from_file_location('p', {str(PLUGIN / '__init__.py')!r}, submodule_search_locations=[{str(PLUGIN)!r}])
m = importlib.util.module_from_spec(spec); sys.modules['p'] = m; spec.loader.exec_module(m)
class C:
    hooks = {{}}
    def get_config(self, k, default=None): return default
    def register_hook(self, n, f): self.hooks[n] = f
c = C(); m.register(c); c.hooks['post_llm_call'](session_id='exit', turn_id='e1', assistant_response='bye')
"""
subprocess.run([sys.executable, "-c", script], check=True)
assert conn.execute("SELECT status FROM events WHERE key='turn:exit:e1'").fetchone()[0] == "done"
print("REGRESSIONS PASSED")
