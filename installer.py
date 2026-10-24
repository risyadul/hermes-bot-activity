"""Track new Bots from the Desktop pane: what install.sh does, callable from the backend.

The plugin manager is per profile home, so each Bot needs its own copy of this package under
``<profile>/plugins/bot-activity`` and the plugin in that profile's ``plugins.enabled``. The
reference is the ROOT copy (``<root>/plugins/bot-activity``) — never the copy the dashboard
happened to mount, which may be a (stale) profile copy.

How a Bot gets its copy depends on how the root copy was installed:

* **From the catalog or a Git URL** (Hermes recorded its provenance): the Bot runs
  ``hermes -p <bot> plugins install <same source> --ref <same commit> --enable`` — the exact
  reviewed commit, with its own install record, so ``hermes plugins update`` keeps working.
* **Manually / from a dev checkout** (no install record): the root files are copied in, the
  way ``hermes plugins install`` would have laid them out, then enabled.

Every CLI call runs out of process, which also asks the running Desktop backend to load the
plugin live (``plugins_activation.notify_serve_backend``), so no restart is needed.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PLUGIN_ID = "bot-activity"
# Never shipped into profile copies, never hashed: the shell installer, interpreter caches, and
# dotfiles (a Finder .DS_Store or editor swap file must not make a copy look outdated).
EXCLUDED = {"__pycache__", "install.sh"}
# Per Bot: CLI start-up + notify_serve_backend (≤60 s) + gateway reload (≤2×30 s).
ENABLE_TIMEOUT_S = 150
# An install also clones the pinned commit first.
INSTALL_TIMEOUT_S = 240
FULL_SHA = re.compile(r"[0-9a-f]{40}")
# Child-process identity the CLI must not inherit from the Desktop serve process.
_CHILD_ENV_DROP = ("HERMES_SPAWN", "HERMES_DASHBOARD_SESSION_TOKEN")
_CHILD_ENV_DROP_PREFIXES = ("HERMES_PARENT_",)
_install_lock = threading.Lock()
_config_cache: dict[Path, tuple[tuple[int, int, int], tuple[frozenset, frozenset]]] = {}


def _ignored(name: str) -> bool:
    return name in EXCLUDED or name.startswith(".")


def _package_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*")
                  if p.is_file() and not any(_ignored(part) for part in p.relative_to(directory).parts))


def package_digest(directory: Path) -> Optional[str]:
    """Content hash of the package, or None when absent/unreadable (e.g. mid-swap)."""
    try:
        if not (directory / "plugin.yaml").is_file():
            return None
        digest = hashlib.sha256()
        for path in _package_files(directory):
            digest.update(str(path.relative_to(directory)).encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()
    except OSError:
        return None


def source_dir(root: Path, fallback: Path) -> Path:
    """The root copy; ``fallback`` (the mounted package) only when the root copy is missing."""
    root_copy = root / "plugins" / PLUGIN_ID
    return root_copy if (root_copy / "plugin.yaml").is_file() else fallback


def install_origin(root: Path) -> Optional[dict[str, Optional[str]]]:
    """How the root copy was installed, from Hermes's install record: ``{"identifier", "ref"}``
    (catalog name, or Git source incl. ``#subdir``; ref = the full pinned commit), or None for a
    manual/dev copy Hermes did not install."""
    try:
        from hermes_cli.plugins_provenance import read_sidecar_rows
    except ImportError:
        return None
    row = read_sidecar_rows(root / "plugins").get(PLUGIN_ID)
    if not isinstance(row, dict):
        return None
    catalog = row.get("catalog") if isinstance(row.get("catalog"), dict) else {}
    identifier = catalog.get("name") or row.get("source")
    if not isinstance(identifier, str) or not identifier.strip():
        return None
    ref = row.get("revision")
    return {"identifier": identifier.strip(), "ref": ref if isinstance(ref, str) and FULL_SHA.fullmatch(ref) else None}


def profile_names(root: Path) -> list[str]:
    """Every live Bot profile, by the same test ``hermes -p`` uses (a fresh ``hermes profile
    create`` has no config.yaml yet, and a deleted profile leaves a tombstoned dir behind)."""
    from hermes_constants import named_profile_is_live

    profiles_dir = root / "profiles"
    names = ["default"]
    if profiles_dir.is_dir():
        names += sorted(p.name for p in profiles_dir.iterdir()
                        if p.is_dir() and not p.name.startswith(".") and named_profile_is_live(p))
    return names


def _home(root: Path, bot: str) -> Path:
    return root if bot == "default" else root / "profiles" / bot


def _plugin_sets(config: Path) -> tuple[frozenset, frozenset]:
    """(enabled, disabled) plugin names from a profile's config.yaml, re-parsed only on change."""
    try:
        st = config.stat()
    except OSError:
        return frozenset(), frozenset()
    stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
    cached = _config_cache.get(config)
    if cached and cached[0] == stamp:
        return cached[1]
    # Hermes's own YAML loader (ruamel-backed, in core since 0.21.6); PyYAML is not shipped.
    import hermes_yaml

    try:
        data = hermes_yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.debug("bot-activity: unreadable %s", config, exc_info=True)
        return frozenset(), frozenset()
    plugins = data.get("plugins") if isinstance(data, dict) else None
    plugins = plugins if isinstance(plugins, dict) else {}

    def names(value: Any) -> frozenset:
        return frozenset(v for v in value if isinstance(v, str)) if isinstance(value, list) else frozenset()

    sets = (names(plugins.get("enabled")), names(plugins.get("disabled")))
    _config_cache[config] = (stamp, sets)
    return sets


def profile_status(root: Path, source: Path, bot: str, source_digest: Optional[str] = None) -> dict[str, Any]:
    home = _home(root, bot)
    target = home / "plugins" / PLUGIN_ID
    enabled, disabled = _plugin_sets(home / "config.yaml")
    installed = (target / "plugin.yaml").is_file()
    is_source = installed and target.resolve() == source.resolve()
    target_digest = None if is_source or not installed else package_digest(target)
    outdated = bool(target_digest) and target_digest != (source_digest or package_digest(source))
    # The user turned the plugin off for this Bot: respect it, never re-enable it on their behalf.
    opted_out = PLUGIN_ID in disabled
    is_enabled = PLUGIN_ID in enabled and not opted_out
    return {"bot": bot, "installed": installed, "enabled": is_enabled, "outdated": outdated,
            "opted_out": opted_out, "tracked": installed and is_enabled}


def setup_status(root: Path, source: Path) -> list[dict[str, Any]]:
    source_digest = package_digest(source)
    return [profile_status(root, source, bot, source_digest) for bot in profile_names(root)]


def _safe_target(root: Path, source: Path, bot: str) -> Path:
    """The Bot's plugin dir, refusing anything that escapes its profile (a symlinked ``plugins``
    shared with the root would make the sync overwrite the source itself)."""
    home = _home(root, bot).resolve()
    target = _home(root, bot) / "plugins" / PLUGIN_ID
    resolved = target.resolve()
    if resolved == source.resolve() or home not in resolved.parents:
        raise RuntimeError("plugin folder is linked outside this profile; sync it manually")
    return target


def _sync_copy(source: Path, target: Path, scratch: Path) -> None:
    """Replace ``target`` with a fresh copy of ``source`` without ever exposing a partial package
    to plugin discovery: stage in ``scratch`` (outside the scanned ``plugins/`` dir, same
    filesystem), then swap with two renames."""
    scratch.mkdir(parents=True, exist_ok=True)
    stage = scratch / f"stage-{uuid.uuid4().hex[:8]}"
    retired = scratch / f"old-{uuid.uuid4().hex[:8]}"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(source, stage, ignore=lambda _d, names: [n for n in names if _ignored(n)])
        if target.exists() or target.is_symlink():
            target.rename(retired)
        stage.rename(target)
    except BaseException:
        if (retired.exists() or retired.is_symlink()) and not target.exists():
            retired.rename(target)
        shutil.rmtree(stage, ignore_errors=True)
        raise
    if retired.is_symlink():
        retired.unlink()
    else:
        shutil.rmtree(retired, ignore_errors=True)


def _hermes_executable() -> str:
    import hermes_cli

    launcher = Path(hermes_cli.__file__).resolve().parents[1] / ".hermes" / "bin" / "hermes"
    executable = str(launcher) if launcher.is_file() else shutil.which("hermes")
    if not executable:
        raise RuntimeError("hermes CLI not found")
    return executable


def _hermes_command(bot: str) -> list[str]:
    # Always an explicit -p, "default" included: without it the CLI follows the sticky
    # ~/.hermes/active_profile and would enable the plugin in whatever profile is active.
    return [_hermes_executable(), "-p", bot, "plugins", "enable", PLUGIN_ID]


def _install_command(bot: str, origin: dict[str, Optional[str]], *, replace: bool) -> list[str]:
    cmd = [_hermes_executable(), "-p", bot, "plugins", "install", origin["identifier"], "--enable", "--yes-deps"]
    if origin.get("ref"):
        cmd += ["--ref", origin["ref"]]
    return cmd + (["--force"] if replace else [])


def _child_env(root: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in _CHILD_ENV_DROP and not k.startswith(_CHILD_ENV_DROP_PREFIXES)}
    env["HERMES_HOME"] = str(root)  # -p resolves the profile against THIS root (custom/Docker roots)
    return env


def _run_cli(root: Path, cmd: list[str], timeout: float) -> None:
    proc = subprocess.run(cmd, env=_child_env(root), capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else f"hermes exited with {proc.returncode}")


def _enable(root: Path, bot: str) -> None:
    _run_cli(root, _hermes_command(bot), ENABLE_TIMEOUT_S)


def track(root: Path, source: Path, bots: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Install/refresh + enable the plugin for ``bots`` (default: every untracked or outdated
    profile). Unknown names are rejected — only profiles that exist on disk are ever touched."""
    with _install_lock:
        known = profile_names(root)
        source_digest = package_digest(source)
        # Unasked, opted-out Bots are left alone silently; asked for by name, they report why.
        targets = ([b for b in known if not profile_status(root, source, b, source_digest)["opted_out"]]
                   if bots is None else bots)
        results = []
        for bot in targets:
            if bot not in known:
                results.append({"bot": bot, "ok": False, "error": "unknown profile"})
                continue
            results.append(_track_one(root, source, bot, source_digest))
        return results


def _track_one(root: Path, source: Path, bot: str, source_digest: Optional[str]) -> dict[str, Any]:
    status = profile_status(root, source, bot, source_digest)
    if status["opted_out"]:
        return {"bot": bot, "ok": False, "error": "disabled for this bot — enable it in Capabilities → Plugins"}
    if status["tracked"] and not status["outdated"]:
        return {"bot": bot, "ok": True, "changed": False}
    origin = install_origin(root)
    try:
        if not status["installed"] or status["outdated"]:
            target = _safe_target(root, source, bot)
            if origin:
                _run_cli(root, _install_command(bot, origin, replace=status["installed"]), INSTALL_TIMEOUT_S)
            else:
                _sync_copy(source, target, _home(root, bot) / "plugin-data" / PLUGIN_ID / ".sync")
        if not profile_status(root, source, bot, source_digest)["enabled"]:
            _enable(root, bot)
    except subprocess.TimeoutExpired:
        # The CLI writes files/config before its slow live-activation round trips; check what landed.
        after = profile_status(root, source, bot, source_digest)
        if after["installed"] and after["enabled"]:
            return {"bot": bot, "ok": True, "changed": True, "note": "enabled; live activation still pending"}
        return {"bot": bot, "ok": False, "error": "timed out installing the plugin"}
    except Exception as exc:
        logger.warning("bot-activity: tracking %s failed: %s", bot, exc)
        return {"bot": bot, "ok": False, "error": str(exc)[:300]}
    return {"bot": bot, "ok": True, "changed": True, "restart_needed": status["outdated"],
            "via": "install" if origin else "copy"}
