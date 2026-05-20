"""Linux systemd ``--user`` integration."""

from __future__ import annotations

import subprocess

from aiguard.constants import AIGuardConstants
from aiguard.installer import paths
from aiguard.installer.templates import render


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        check=check,
        capture_output=True,
        text=True,
    )


def install() -> None:
    unit_path = paths.systemd_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(
        render(
            "ai-guard.service.in",
            WRAPPER=str(paths.wrapper_path()),
            LOG=str(paths.service_log_file_path()),
        ),
        encoding="utf-8",
    )
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", AIGuardConstants.SYSTEMD_UNIT_NAME)


def uninstall() -> None:
    unit_path = paths.systemd_unit_path()
    # Stop + disable best-effort; if the unit doesn't exist that's fine.
    _systemctl("disable", "--now", AIGuardConstants.SYSTEMD_UNIT_NAME, check=False)
    try:
        unit_path.unlink()
    except FileNotFoundError:
        pass
    _systemctl("daemon-reload", check=False)


def is_running() -> bool:
    result = _systemctl("is-active", AIGuardConstants.SYSTEMD_UNIT_NAME, check=False)
    return result.stdout.strip() == "active"
