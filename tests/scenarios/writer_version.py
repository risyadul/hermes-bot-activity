"""A plugin copy with a different writer replaces the live one; old hooks follow the new writer."""
import importlib.util, os, sqlite3, sys, tempfile, time
from pathlib import Path
home = Path(tempfile.mkdtemp(prefix="hermes-wv-")); os.environ["HERMES_HOME"] = str(home)
PLUGIN = Path(__file__).resolve().parents[2]
def load(name):
    spec = importlib.util.spec_from_file_location(name, PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)])
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m); return m
class Ctx:
    def __init__(self): self.hooks = {}
    def get_config(self, k, default=None): return default
    def register_hook(self, n, f): self.hooks[n] = f
a = load("copy_a"); ca = Ctx(); a.register(ca)
first = sys.modules["_bot_activity_runtime"].writer
b = load("copy_b"); sys.modules["copy_b.store"].WRITER_VERSION = "different"
cb = Ctx(); b.register(cb)
second = sys.modules["_bot_activity_runtime"].writer
assert second is not first
ca.hooks["pre_tool_call"](tool_name="t", args={}, session_id="s", turn_id="t", tool_call_id="from-old-hooks")
time.sleep(1.0)
first._thread.join(2); assert not first._thread.is_alive(), "retired writer stops"
db = sqlite3.connect(home / "plugin-data/bot-activity/activity.db")
assert db.execute("SELECT count(*) FROM events WHERE key='tool:from-old-hooks'").fetchone()[0] == 1
print("WRITER VERSION PASSED")
