"""Generate ``~/.local/bin/ai-guard-service``.

The launchd plist and the systemd unit both call this wrapper, which sources
``$XDG_CONFIG_HOME/ai-guard/config.env`` before exec'ing the ``ai-guard proxy``
command. Keeps the service files dumb and means config never ends up in
world-readable plists.

DD_API_KEY / DD_APP_KEY normally live in the OS keychain, not config.env (see
:mod:`aiguard.keychain`); ``ai-guard proxy`` loads them into the environment at
startup. On a host with no keychain they fall back to config.env and the
``set -a`` sourcing below exports them like any other value.
"""

from __future__ import annotations

import os

from aiguard import paths
from aiguard.installer.templates import render


def write() -> None:
    content = render(
        "ai-guard-service.sh.in",
        CONFIG_ENV=str(paths.config_env_path()),
        BINARY=str(paths.binary_path()),
    )
    target = paths.wrapper_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    os.chmod(target, 0o755)


def remove() -> None:
    target = paths.wrapper_path()
    try:
        target.unlink()
    except FileNotFoundError:
        pass
