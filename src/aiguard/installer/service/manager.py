"""Platform-dispatched service operations."""

from __future__ import annotations

from aiguard.installer import paths
from aiguard.installer.service import launchd, systemd_user, wrapper


def _backend():
    if paths.is_macos():
        return launchd
    if paths.is_linux():
        return systemd_user
    raise RuntimeError(f"unsupported platform: {paths.is_macos()=}, {paths.is_linux()=}")


def install() -> None:
    wrapper.write()
    _backend().install()


def uninstall() -> None:
    _backend().uninstall()
    wrapper.remove()


def is_running() -> bool:
    return _backend().is_running()
