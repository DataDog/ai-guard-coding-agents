"""Filesystem locations the installer reads and writes.

Single source of truth so tests can monkeypatch ``Path.home`` and every helper
follows along.
"""

from __future__ import annotations

import sys
from pathlib import Path

from aiguard.constants import AIGuardConstants


def home() -> Path:
    return Path.home()


def state_dir() -> Path:
    return home() / ".ai_guard"


def backups_dir() -> Path:
    return state_dir() / "backups"


def restore_state_path() -> Path:
    return backups_dir() / "restore-state.json"


def config_env_path() -> Path:
    return state_dir() / "config.env"


def log_file_path() -> Path:
    return state_dir() / "ai_guard.log"


def service_log_file_path() -> Path:
    return state_dir() / "ai_guard_service.log"


def local_bin_dir() -> Path:
    return home() / ".local" / "bin"


def binary_path() -> Path:
    return local_bin_dir() / "ai-guard"


def wrapper_path() -> Path:
    return local_bin_dir() / "ai-guard-service"


def launchd_plist_path() -> Path:
    return home() / "Library" / "LaunchAgents" / f"{AIGuardConstants.LAUNCHD_LABEL}.plist"


def systemd_unit_path() -> Path:
    return home() / ".config" / "systemd" / "user" / AIGuardConstants.SYSTEMD_UNIT_NAME


def claude_settings_path() -> Path:
    return home() / ".claude" / "settings.json"


def proxy_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")
