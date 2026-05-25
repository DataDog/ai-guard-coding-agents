"""macOS launchd (LaunchAgent) integration."""

from __future__ import annotations

import os
import subprocess

from aiguard import paths
from aiguard.constants import AIGuardConstants
from aiguard.installer.templates import render


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["launchctl", *args],
        check=check,
        capture_output=True,
        text=True,
    )


def install(host: str, port: int) -> None:
    plist_path = paths.launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(
        render(
            "com.datadoghq.ai-guard.plist.in",
            LABEL=AIGuardConstants.LAUNCHD_LABEL,
            WRAPPER=str(paths.wrapper_path()),
            HOME=str(paths.home()),
            SOCKET_NAME=AIGuardConstants.LAUNCHD_SOCKET_NAME,
            HOST=host,
            PORT=str(port),
        ),
        encoding="utf-8",
    )

    # `launchctl bootstrap` is the modern path (macOS 10.10+). It rejects a
    # service that's already loaded, so unload first if needed.
    _launchctl("bootout", f"{_domain()}/{AIGuardConstants.LAUNCHD_LABEL}", check=False)
    result = _launchctl("bootstrap", _domain(), str(plist_path), check=False)
    if result.returncode != 0:
        # Fall back to the legacy syntax (older macOS).
        legacy = _launchctl("load", "-w", str(plist_path), check=False)
        if legacy.returncode != 0:
            raise RuntimeError(
                f"failed to register LaunchAgent: {result.stderr.strip() or legacy.stderr.strip()}"
            )


def uninstall() -> None:
    plist_path = paths.launchd_plist_path()
    _launchctl("bootout", f"{_domain()}/{AIGuardConstants.LAUNCHD_LABEL}", check=False)
    _launchctl("unload", str(plist_path), check=False)
    try:
        plist_path.unlink()
    except FileNotFoundError:
        pass


def is_running() -> bool:
    result = _launchctl("print", f"{_domain()}/{AIGuardConstants.LAUNCHD_LABEL}", check=False)
    return result.returncode == 0
