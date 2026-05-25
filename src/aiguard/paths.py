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


def state_dir() -> Path:
    return Path(os.environ.get("DD_AI_GUARD_HOME") or (Path.home() / ".ai_guard"))


def config_env_path() -> Path:
    return state_dir() / "config.env"


def log_file_path() -> Path:
    return state_dir() / "ai_guard.log"


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


def systemd_unit_path() -> Path:
    return home() / ".config" / "systemd" / "user" / AIGuardConstants.SYSTEMD_UNIT_NAME


def systemd_socket_path() -> Path:
    return home() / ".config" / "systemd" / "user" / AIGuardConstants.SYSTEMD_SOCKET_NAME


def claude_settings_path() -> Path:
    return home() / ".claude" / "settings.json"
