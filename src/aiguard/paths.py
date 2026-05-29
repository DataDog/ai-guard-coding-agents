"""Filesystem locations the installer reads and writes.

Single source of truth so tests can monkeypatch ``Path.home`` and every helper
follows along.
"""

from __future__ import annotations

import os
from pathlib import Path

from aiguard.constants import AIGuardConstants


def home() -> Path:
    return Path.home()


def config_home() -> Path:
    """``$XDG_CONFIG_HOME`` or ``~/.config``."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")).expanduser()


def ai_guard_config_dir() -> Path:
    """``$XDG_CONFIG_HOME/ai-guard`` — user-facing configuration."""
    return config_home() / "ai-guard"


def state_dir() -> Path:
    """``$XDG_STATE_HOME/ai-guard`` — runtime state (logs + session history).

    ``DD_AI_GUARD_HOME`` overrides this wholesale; tests use it to point
    storage at a sandboxed tmpdir.
    """
    if explicit := os.environ.get("DD_AI_GUARD_HOME"):
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base).expanduser() / "ai-guard"


def config_env_path() -> Path:
    return ai_guard_config_dir() / "config.env"


def log_file_path() -> Path:
    return state_dir() / "ai-guard.log"


def local_bin_dir() -> Path:
    return home() / ".local" / "bin"


def bundle_dir() -> Path:
    """Root of the PyInstaller onedir bundle.

    The launcher executable plus its ``_internal/`` siblings live here; the
    user-facing ``binary_path()`` is a symlink into this directory.
    """
    return home() / ".local" / "share" / "ai-guard"


def bundle_executable() -> Path:
    """The PyInstaller launcher inside the bundle (the real exec target)."""
    return bundle_dir() / "ai-guard"


def binary_path() -> Path:
    """User-facing entry on ``PATH``; symlink to :func:`bundle_executable`."""
    return local_bin_dir() / "ai-guard"


def wrapper_path() -> Path:
    return local_bin_dir() / "ai-guard-service"


def launchd_plist_path() -> Path:
    return home() / "Library" / "LaunchAgents" / f"{AIGuardConstants.LAUNCHD_LABEL}.plist"


def systemd_path() -> Path:
    return config_home() / "systemd" / "user"


def systemd_unit_path() -> Path:
    return systemd_path() / AIGuardConstants.SYSTEMD_UNIT_NAME


def systemd_socket_path() -> Path:
    return systemd_path() / AIGuardConstants.SYSTEMD_SOCKET_NAME


def claude_config_dir() -> Path:
    """Claude Code's config directory."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if not override:
        # Lazy import: storage imports paths, so a module-level import cycles.
        from aiguard import storage

        override = storage.load_config().get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return home() / ".claude"


def claude_settings_path() -> Path:
    return claude_config_dir() / "settings.json"
