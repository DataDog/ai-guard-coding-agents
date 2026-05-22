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
