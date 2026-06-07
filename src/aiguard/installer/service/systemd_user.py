"""Linux systemd ``--user`` integration."""

from __future__ import annotations

import subprocess

from aiguard import paths
from aiguard.constants import AIGuardConstants


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        check=check,
        capture_output=True,
        text=True,
    )


def uninstall() -> None:
    unit_path = paths.systemd_unit_path()
    socket_path = paths.systemd_socket_path()
    # Stop + disable best-effort; if the units don't exist that's fine.
    _systemctl("stop", AIGuardConstants.SYSTEMD_UNIT_NAME, check=False)
    _systemctl("disable", "--now", AIGuardConstants.SYSTEMD_SOCKET_NAME, check=False)
    for path in (socket_path, unit_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    _systemctl("daemon-reload", check=False)
