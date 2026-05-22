# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Per-session conversation message storage.

Layout: ``~/.ai_guard/<agent>/<session_id>.json`` — one JSON array of Message dicts
per session, in observation order. The root can be overridden via
``DD_AI_GUARD_HOME``.

``session_id`` and ``agent`` flow in from request metadata that the proxy does
not control, so the resolved path is checked to stay within the storage root;
anything else short-circuits as if the session did not exist (load returns
``[]``, save/delete are no-ops + log).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
from pathlib import Path

from ddtrace.appsec.ai_guard import Message

from aiguard import paths
from aiguard.paths import state_dir
from aiguard.utils import atomic_write

logger = logging.getLogger("ai_guard")

_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _session_file(agent: str, session_id: str) -> Path | None:
    """Return the absolute path of a session's JSON file.

    ``None`` if the resolved path would escape the storage root (defense against
    path traversal via attacker-controlled ``agent``/``session_id``).
    """
    try:
        root = state_dir().resolve(strict=False)
        candidate = (root / agent / f"{session_id}.json").resolve(strict=False)
        candidate.relative_to(root)
    except (OSError, ValueError):
        logger.warning(
            "storage: rejecting agent/session_id that escapes the storage root "
            "(agent=%r session_id=%r)",
            agent,
            session_id,
        )
        return None
    return candidate


def _quote(value: str) -> str:
    """Return the shell-safe representation of ``value`` for ``. config.env``.

    The wrapper script sources this file with ``set -a; . config.env; set +a``,
    so values have to survive POSIX shell parsing.
    """
    if value == "":
        return '""'
    return shlex.quote(value)


def load_messages(agent: str, session_id: str) -> list[Message]:
    """Return the full message list for a session. ``[]`` if absent or unreadable."""
    if not agent or not session_id:
        return []
    path = _session_file(agent, session_id)
    if path is None or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.error("failed to read %s", path, exc_info=True)
        return []
    return data if isinstance(data, list) else []


def save_messages(agent: str, session_id: str, messages: list[Message]) -> None:
    """Overwrite the session file with ``messages``."""
    if not agent or not session_id or messages is None:
        return
    path = _session_file(agent, session_id)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(messages, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        logger.error("failed to write %s", path, exc_info=True)


def delete_messages(agent: str, session_id: str) -> None:
    """Remove the session file. No-op if it doesn't exist."""
    if not agent or not session_id:
        return
    path = _session_file(agent, session_id)
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.error("failed to delete %s", path, exc_info=True)


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
