"""Versioned backups of agent config files + restore-state metadata.

Each install snapshots the original agent config to
``~/.ai_guard/backups/<agent>-settings.<ISO>.json`` and records, in
``restore-state.json``, an opaque per-agent ``restore_data`` dict (e.g. a
pre-existing ``ANTHROPIC_BASE_URL`` we chained through) that uninstall hands
back to the agent module verbatim.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from aiguard.installer import paths


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def snapshot(agent: str, source: Path) -> Path | None:
    """Copy ``source`` into the backups dir; return the new path (or None).

    Skips and returns the existing snapshot if one already exists for this
    agent. The first install captures the pristine config; subsequent
    re-installs leave that pristine snapshot alone so it can't be rotated
    out by repeated runs. ``uninstall`` clears the backups dir, so a fresh
    install snapshots again from scratch.
    """
    if not source.exists():
        return None
    backups_dir = paths.backups_dir()
    backups_dir.mkdir(parents=True, exist_ok=True)

    suffix = source.suffix or ".bak"
    existing = sorted(backups_dir.glob(f"{agent}-settings.*{suffix}"))
    if existing:
        return existing[0]

    dest = backups_dir / f"{agent}-settings.{_timestamp()}{suffix}"
    shutil.copy2(source, dest)
    return dest


def _read_restore_state() -> dict:
    path = paths.restore_state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write_restore_state(data: dict) -> None:
    path = paths.restore_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=".restore-state.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def record_install(
    agent: str,
    settings_path: Path,
    restore_data: dict[str, str],
) -> None:
    """Persist the install record, preserving prior ``restore_data`` on re-install.

    On second install the agent has nothing to chain (its upstream is already
    our proxy), so ``restore_data`` arrives empty. We must keep the original
    chained-upstream value from the first install so uninstall can put it
    back. Existing keys win; new non-empty keys are added.
    """
    state = _read_restore_state()
    prior = (state.get(agent) or {}).get("restore_data") or {}
    merged = dict(prior)
    for key, value in (restore_data or {}).items():
        if value:
            merged.setdefault(key, value)
    state[agent] = {
        "settings_path": str(settings_path),
        "restore_data": merged,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_restore_state(state)


def load_install(agent: str) -> dict | None:
    return _read_restore_state().get(agent)


def all_agents() -> list[str]:
    return sorted(_read_restore_state().keys())


def clear() -> None:
    """Remove the entire backups directory (called from uninstall)."""
    backups_dir = paths.backups_dir()
    if backups_dir.exists():
        shutil.rmtree(backups_dir)
