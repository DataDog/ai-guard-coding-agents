"""Generate ``~/.local/bin/ai-guard-service``.

The launchd plist and the systemd unit both call this wrapper, which sources
``~/.ai_guard/config.env`` (containing secrets and config) before exec'ing the
``ai-guard proxy`` command. Keeps the service files dumb and means secrets
never end up in world-readable plists.
"""

from __future__ import annotations

import os

from aiguard.installer import paths
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
