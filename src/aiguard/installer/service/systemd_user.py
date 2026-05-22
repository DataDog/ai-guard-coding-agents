"""Linux systemd ``--user`` integration."""

from __future__ import annotations

import subprocess

from aiguard import paths
from aiguard.constants import AIGuardConstants
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
    socket_path = paths.systemd_socket_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(
        render(
            "ai-guard.service.in",
            WRAPPER=str(paths.wrapper_path()),
            SOCKET_NAME=AIGuardConstants.SYSTEMD_SOCKET_NAME,
        ),
        encoding="utf-8",
    )
    socket_path.write_text(
        render(
            "ai-guard.socket.in",
            HOST=AIGuardConstants.PROXY_HOST_DEFAULT,
            PORT=str(AIGuardConstants.PROXY_PORT_DEFAULT),
            SERVICE_NAME=AIGuardConstants.SYSTEMD_UNIT_NAME,
        ),
        encoding="utf-8",
    )
    _systemctl("daemon-reload")
    # Enable the SOCKET, not the service: socket-activation means the service
    # starts on demand when something connects to the listening port.
    _systemctl("enable", "--now", AIGuardConstants.SYSTEMD_SOCKET_NAME)


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


def is_running() -> bool:
    # With socket activation the service is on-demand, so "is the proxy
    # installed and reachable" is really "is the socket listening". The
    # service unit may be ``inactive`` between requests — that's expected.
    result = _systemctl("is-active", AIGuardConstants.SYSTEMD_SOCKET_NAME, check=False)
    return result.stdout.strip() == "active"
