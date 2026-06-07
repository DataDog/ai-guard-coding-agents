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


def uninstall() -> None:
    _backend().uninstall()
    wrapper.remove()
