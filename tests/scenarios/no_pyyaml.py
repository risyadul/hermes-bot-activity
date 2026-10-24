"""A stock Hermes install has no PyYAML: setup status and room/Bot names must still work."""
import importlib.util, sys, tempfile
from pathlib import Path

# Simulate the stock install before anything can import it: `import yaml` now raises ImportError.
sys.modules["yaml"] = None

PLUGIN = Path(__file__).resolve().parents[2]

def load(name):
    spec = importlib.util.spec_from_file_location(name, PLUGIN / f"{name}.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

installer, rooms = load("installer"), load("rooms")

root = Path(tempfile.mkdtemp(prefix="hermes-noyaml-"))
source = root / "plugins" / "bot-activity"
source.mkdir(parents=True); (source / "plugin.yaml").write_text("name: bot-activity\n")
(root / "config.yaml").write_text("plugins:\n  enabled:\n    - bot-activity\n  disabled: []\n")
(root / "profile.yaml").write_text("""
ui_meta:
  hermes-bots: {title: Principal Engineer}
  hermes-bots-groups:
    rooms:
      id:rmA:
        name: Android Squad
        roomId: rmA
        members: [{name: default}]
        log:
          - {thread: tmA1, from: {kind: user, name: You}, text: Build the release APK}
""")

default = installer.profile_status(root, source, "default")
assert default["enabled"] and default["tracked"], default
assert rooms.bot_title(root, "default") == "Principal Engineer"
room, = rooms.group_rooms(root)
assert room["name"] == "Android Squad" and room["threads"] == {"tmA1": "Build the release APK"}, room
try:
    import yaml  # noqa: F401
    raise AssertionError("PyYAML should be unavailable in this scenario")
except ImportError:
    pass
print("NO-PYYAML PASSED")
