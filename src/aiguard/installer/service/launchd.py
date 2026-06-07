"""macOS launchd (LaunchAgent) integration."""

from __future__ import annotations

import os
import subprocess

from aiguard import paths
from aiguard.constants import AIGuardConstants


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["launchctl", *args],
        check=check,
        capture_output=True,
        text=True,
    )


def uninstall() -> None:
    plist_path = paths.launchd_plist_path()
    _launchctl("bootout", f"{_domain()}/{AIGuardConstants.LAUNCHD_LABEL}", check=False)
    _launchctl("unload", str(plist_path), check=False)
    try:
        plist_path.unlink()
    except FileNotFoundError:
        pass
