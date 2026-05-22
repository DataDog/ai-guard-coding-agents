"""Platform-dispatched service operations."""

from __future__ import annotations

import sys

from aiguard import utils
from aiguard.installer.service import launchd, systemd_user, wrapper


def _backend():
    if utils.is_macos():
        return launchd
    if utils.is_linux():
        return systemd_user
    raise RuntimeError(f"unsupported platform: {sys.platform}")


def install() -> None:
    wrapper.write()
    _backend().install()


def uninstall() -> None:
    _backend().uninstall()
    wrapper.remove()


def is_running() -> bool:
    return _backend().is_running()


def log_hint() -> str:
    """User-facing command for tailing the service log on the current platform."""
    return _backend().log_hint()


def tail_log(lines: int = 50) -> tuple[str, str]:
    """Return ``(title, body)`` for the most-recent service log entries.

    The body is whatever the platform's canonical reader returns; ``title`` is
    the shell command that produced it (so it can be shown back to the user as
    the panel header).
    """
    return _backend().tail_log(lines)
