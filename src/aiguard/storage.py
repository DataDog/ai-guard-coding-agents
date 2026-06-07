# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""``config.env`` storage.

Reads and writes ``$XDG_CONFIG_HOME/ai-guard/config.env`` — the ``DD_*`` keys
ai-guard needs (API keys, site, block mode, …). The file is written with
POSIX-shell quoting and locked to user-only permissions.
"""

from __future__ import annotations

import logging
import re
import shlex
from pathlib import Path

from aiguard import paths
from aiguard.utils import atomic_write

logger = logging.getLogger("ai_guard")

_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def load_into_environ() -> None:
    """Apply ``config.env`` to ``os.environ`` without overwriting existing vars.

    Entry points that run outside the install shell (the ``hook`` subprocess,
    the CLI) call this so the ``DD_*`` keys written at install time are present.
    Anything already exported by the caller wins; best-effort, never raises.
    """
    import os

    try:
        values = load_config()
    except Exception:
        logger.debug("could not load %s", paths.config_env_path(), exc_info=True)
        return
    for key, value in values.items():
        os.environ.setdefault(key, value)


def _quote(value: str) -> str:
    """Return the shell-safe representation of ``value`` for ``. config.env``.

    The wrapper script sources this file with ``set -a; . config.env; set +a``,
    so values have to survive POSIX shell parsing.
    """
    if value == "":
        return '""'
    return shlex.quote(value)


def load_config(path: Path | None = None) -> dict[str, str]:
    target = path or paths.config_env_path()
    if not target.exists():
        return {}

    """Tolerant parser, sufficient for files we wrote with :func:`serialize`."""
    out: dict[str, str] = {}
    text = target.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not _KEY_RE.match(key):
            continue
        # Use shlex to undo the quoting applied by save_config().
        parts = shlex.split(value, comments=False, posix=True)
        out[key] = parts[0] if parts else ""
    return out


def save_config(values: dict[str, str], path: Path | None = None) -> None:
    target = path or paths.config_env_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for key, value in values.items():
        if not _KEY_RE.match(key):
            raise ValueError(f"refusing to write malformed env var name: {key!r}")
        lines.append(f"{key}={_quote(value)}")
    payload = "\n".join(lines) + "\n"
    # config.env carries DD_API_KEY / DD_APP_KEY — lock it to user-only.
    atomic_write(target, lambda fh: fh.write(payload), mode=0o600)
