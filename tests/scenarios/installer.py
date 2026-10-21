"""Track-new-bots: status, safe atomic copy, CLI invocation (mocked run), review findings."""
import importlib.util, os, shutil, subprocess, sys, tempfile
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("installer", PLUGIN / "installer.py")
inst = importlib.util.module_from_spec(spec); spec.loader.exec_module(inst)

root = Path(tempfile.mkdtemp(prefix="hermes-inst-"))
source = root / "plugins" / "bot-activity"
shutil.copytree(PLUGIN, source, ignore=shutil.ignore_patterns("__pycache__", ".git", "tests"))
(source / "__pycache__").mkdir(); (source / "__pycache__" / "junk.pyc").write_text("x")
ENABLED = "plugins:\n  enabled:\n    - bot-activity\n  disabled: []\n"
(root / "config.yaml").write_text(ENABLED)
def profile(bot, cfg="plugins:\n  enabled: []\n", copy=False):
    (root / "profiles" / bot).mkdir(parents=True)
    (root / "profiles" / bot / "config.yaml").write_text(cfg)
    if copy:
        shutil.copytree(source, root / "profiles" / bot / "plugins" / "bot-activity", ignore=shutil.ignore_patterns("__pycache__", "install.sh"))
profile("fresh"); profile("stale", ENABLED, copy=True); profile("current", ENABLED, copy=True)
profile("optout", "plugins:\n  enabled: []\n  disabled: [bot-activity]\n", copy=True)
(root / "profiles" / "stale" / "plugins" / "bot-activity" / "store.py").write_text("# old version\n")
(root / "profiles" / "current" / "plugins" / "bot-activity" / ".DS_Store").write_text("finder junk")
(root / "profiles" / ".deleted").mkdir(); (root / "profiles" / "no-config").mkdir()
# What `hermes profile create` (no --clone) leaves: .env + SOUL.md, no config.yaml yet.
(root / "profiles" / "just-created").mkdir(); (root / "profiles" / "just-created" / ".env").write_text("")
(root / "profiles" / "just-created" / "SOUL.md").write_text("bot")

# source_dir: the ROOT copy, even when the mounted package is a (stale) profile copy.
assert inst.source_dir(root, root / "profiles" / "stale" / "plugins" / "bot-activity") == source
status = {s["bot"]: s for s in inst.setup_status(root, source)}
print({b: (s["installed"], s["enabled"], s["outdated"], s["opted_out"], s["tracked"]) for b, s in status.items()})
assert set(status) == {"default", "fresh", "stale", "current", "optout", "just-created"}, set(status)
assert not status["just-created"]["installed"] and not status["just-created"]["tracked"]
assert status["default"]["tracked"] and not status["default"]["outdated"]
assert status["current"]["tracked"] and not status["current"]["outdated"], "dotfiles must not make a copy outdated"
assert status["stale"]["tracked"] and status["stale"]["outdated"]
assert not status["fresh"]["installed"] and status["optout"]["opted_out"] and not status["optout"]["tracked"]

calls = []
def fake_run(cmd, env, **kw):
    calls.append((cmd, env))
    bot = cmd[cmd.index("-p") + 1]
    cfg = root / "config.yaml" if bot == "default" else root / "profiles" / bot / "config.yaml"
    cfg.write_text(ENABLED)
    return subprocess.CompletedProcess(cmd, 0, "", "")
env_in = {"HERMES_HOME": "/somewhere/else", "HERMES_SPAWN": "serve", "HERMES_PARENT_PID": "1",
          "HERMES_PARENT_NONCE": "n", "HERMES_DASHBOARD_SESSION_TOKEN": "secret", "PATH": os.environ["PATH"]}
(root / "active_profile").write_text("current\n")   # sticky profile must not redirect the enable
with mock.patch.object(inst.subprocess, "run", side_effect=fake_run), mock.patch.dict(os.environ, env_in, clear=True):
    by = {r["bot"]: r for r in inst.track(root, source)}
print(by)
assert by["current"] == {"bot": "current", "ok": True, "changed": False}
assert by["fresh"]["ok"] and by["stale"]["ok"] and by["stale"]["restart_needed"]
assert "optout" not in by; r_opt = inst.track(root, source, ["optout"])[0]; assert not r_opt["ok"] and "disabled for this bot" in r_opt["error"]
assert sorted(c[0][c[0].index("-p") + 1] for c in calls) == ["fresh", "just-created"], [c[0] for c in calls]
cmd, env = next(c for c in calls if "fresh" in c[0])
assert cmd[1:] == ["-p", "fresh", "plugins", "enable", "bot-activity"] and Path(cmd[0]).name == "hermes"
assert env["HERMES_HOME"] == str(root) and not {"HERMES_SPAWN", "HERMES_PARENT_PID", "HERMES_PARENT_NONCE", "HERMES_DASHBOARD_SESSION_TOKEN"} & set(env), "child env not sanitized"
assert inst._hermes_command("default")[1:] == ["-p", "default", "plugins", "enable", "bot-activity"]
assert "optout" in (root / "profiles/optout/config.yaml").read_text() or "disabled: [bot-activity]" in (root / "profiles/optout/config.yaml").read_text()

copy = root / "profiles" / "fresh" / "plugins" / "bot-activity"
assert not (copy / "__pycache__").exists() and not (copy / "install.sh").exists() and (copy / "desktop" / "plugin.js").is_file()
assert inst.package_digest(copy) == inst.package_digest(source)
assert [p.name for p in (root / "profiles" / "stale" / "plugins").iterdir()] == ["bot-activity"], "no stage/old dirs in plugins/"

# Symlinked plugins dir pointing at the root plugins: refuse instead of overwriting the source.
(root / "profiles" / "linked").mkdir(); (root / "profiles" / "linked" / "config.yaml").write_text("{}\n")
os.symlink(root / "plugins", root / "profiles" / "linked" / "plugins")
before = inst.package_digest(source)
with mock.patch.object(inst.subprocess, "run", side_effect=fake_run):
    res = inst.track(root, source, ["linked"])[0]
assert res["ok"] and inst.package_digest(source) == before, res        # shares the source: enable only, no copy
# A plugins dir linked OUTSIDE the profile holding an outdated copy: never write through the link.
outside = root / "elsewhere"; (outside / "bot-activity").mkdir(parents=True)
shutil.copytree(source, outside / "bot-activity", dirs_exist_ok=True); (outside / "bot-activity" / "store.py").write_text("# old\n")
(root / "profiles" / "linked2").mkdir(); (root / "profiles" / "linked2" / "config.yaml").write_text(ENABLED)
os.symlink(outside, root / "profiles" / "linked2" / "plugins")
res = inst.track(root, source, ["linked2"])[0]
assert not res["ok"] and "linked outside" in res["error"], res
assert (outside / "bot-activity" / "store.py").read_text() == "# old\n" and inst.package_digest(source) == before

# copytree failure: error reported, no partial package anywhere discoverable, retry possible.
profile("broken")
with mock.patch.object(inst.shutil, "copytree", side_effect=shutil.Error("disk full")):
    res = inst.track(root, source, ["broken"])[0]
assert not res["ok"] and "disk full" in res["error"]
assert not (root / "profiles/broken/plugins/bot-activity").exists()
sync_dir = root / "profiles/broken/plugin-data/bot-activity/.sync"
assert not list(sync_dir.iterdir()), list(sync_dir.iterdir())

# Timeout: config already written by the CLI -> success; nothing written -> failure.
profile("slow"); profile("stuck")
def slow_run(cmd, env, **kw):
    bot = cmd[cmd.index("-p") + 1]
    if bot == "slow":
        (root / "profiles/slow/config.yaml").write_text(ENABLED)
    raise subprocess.TimeoutExpired(cmd, 150)
with mock.patch.object(inst.subprocess, "run", side_effect=slow_run):
    r = {x["bot"]: x for x in inst.track(root, source, ["slow", "stuck"])}
assert r["slow"]["ok"] and "pending" in r["slow"]["note"] and not r["stuck"]["ok"], r
assert not inst.track(root, source, ["../../etc"])[0]["ok"]

# Provenance: when Hermes installed the root copy (Git or catalog), new Bots get the SAME source
# and commit through `hermes plugins install`, never a file copy.
import json
SHA = "0123456789abcdef0123456789abcdef01234567"
def fake_install(cmd, env, **kw):
    calls.append((cmd, env))
    bot = cmd[cmd.index("-p") + 1]
    home = root / "profiles" / bot
    if "install" in cmd:
        shutil.copytree(source, home / "plugins" / "bot-activity", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "install.sh"))
    (home / "config.yaml").write_text(ENABLED)
    return subprocess.CompletedProcess(cmd, 0, "", "")
for row, identifier in (
    ({"source": "https://github.com/acme/hermes-bot-activity", "revision": SHA, "pinned": True},
     "https://github.com/acme/hermes-bot-activity"),
    ({"source": "https://github.com/acme/hermes-bot-activity", "revision": SHA,
      "catalog": {"name": "bot-activity", "pin": SHA, "sha": SHA}}, "bot-activity"),
):
    (root / "plugins" / ".install-metadata.json").write_text(json.dumps({"bot-activity": row}))
    assert inst.install_origin(root) == {"identifier": identifier, "ref": SHA}
    bot = f"git-{len(calls)}"
    profile(bot)
    calls.clear()
    with mock.patch.object(inst.subprocess, "run", side_effect=fake_install):
        r = inst.track(root, source, [bot])[0]
    assert r["ok"] and r["via"] == "install", r
    (cmd, env), = calls
    assert cmd[1:] == ["-p", bot, "plugins", "install", identifier, "--enable", "--yes-deps", "--ref", SHA], cmd
    assert env["HERMES_HOME"] == str(root)
# Outdated install-tracked copy: reinstall in place (--force), same pin.
stale_copy = root / "profiles" / bot / "plugins" / "bot-activity" / "store.py"
stale_copy.write_text("# older\n")
calls.clear()
with mock.patch.object(inst.subprocess, "run", side_effect=fake_install):
    r = inst.track(root, source, [bot])[0]
assert r["ok"] and calls[0][0][-1] == "--force", [c[0] for c in calls]
# A non-sha revision is never passed as --ref (it would not be an exact pin).
(root / "plugins" / ".install-metadata.json").write_text(json.dumps({"bot-activity": {"source": "file:///x", "revision": "main"}}))
assert inst.install_origin(root) == {"identifier": "file:///x", "ref": None}
(root / "plugins" / ".install-metadata.json").unlink()
assert inst.install_origin(root) is None
print("INSTALLER PASSED")
