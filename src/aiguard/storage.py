# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Per-session conversation message storage.

Layout: ``$XDG_STATE_HOME/ai-guard/<agent>/<session_id>/<slot>.json`` — one JSON
array of Message dicts per slot, in observation order. The slot is ``main`` for
the parent session and the subagent's ``agent_id`` for sidechain calls, so
subagent and main-session histories never overwrite each other. The state root
can be overridden via ``DD_AI_GUARD_HOME``.

``agent``, ``session_id`` and ``agent_id`` flow in from request metadata that
the proxy does not control, so the resolved path is checked to stay within the
storage root; anything else short-circuits as if the session did not exist
(load returns ``[]``, save/delete are no-ops + log).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
from pathlib import Path

from ddtrace.appsec.ai_guard import Message

from aiguard import paths
from aiguard.paths import state_dir
from aiguard.utils import atomic_write

logger = logging.getLogger("ai_guard")

_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# Slot name used for the parent session inside ``<agent>/<session_id>/``.
# Subagents land in sibling files named after their ``agent_id``.
_MAIN_SLOT = "main"


def _session_file(agent: str, session_id: str, agent_id: str = "") -> Path | None:
    """Return the absolute path of a session slot's JSON file.

    Layout is ``<root>/<agent>/<session_id>/<slot>.json``, with ``slot`` set to
    the subagent ``agent_id`` for sidechain traffic and ``main`` for the
    parent session. ``None`` if the resolved path would escape the storage
    root (defense against path traversal via attacker-controlled
    ``agent``/``session_id``/``agent_id``).
    """
    slot = agent_id or _MAIN_SLOT
    try:
        root = state_dir().resolve(strict=False)
        candidate = (root / agent / session_id / f"{slot}.json").resolve(strict=False)
        candidate.relative_to(root)
    except (OSError, ValueError):
        logger.warning(
            "storage: rejecting agent/session_id/agent_id that escapes the storage root "
            "(agent=%r session_id=%r agent_id=%r)",
            agent,
            session_id,
            agent_id,
        )
        return None
    return candidate


def _session_dir(agent: str, session_id: str) -> Path | None:
    """Return the directory holding every slot file for a session, or ``None``
    if the resolved path would escape the storage root."""
    try:
        root = state_dir().resolve(strict=False)
        candidate = (root / agent / session_id).resolve(strict=False)
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


def load_messages(agent: str, session_id: str, agent_id: str = "") -> list[Message]:
    """Return the full message list for a session slot. ``[]`` if absent or unreadable.

    ``agent_id`` selects the subagent slot; the empty string targets the parent
    session.
    """
    if not agent or not session_id:
        return []
    path = _session_file(agent, session_id, agent_id)
    if path is None or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.error("failed to read %s", path, exc_info=True)
        return []
    return data if isinstance(data, list) else []


def save_messages(
    agent: str, session_id: str, messages: list[Message], *, agent_id: str = ""
) -> None:
    """Overwrite the slot file for ``(session_id, agent_id)`` with ``messages``."""
    if not agent or not session_id or messages is None:
        return
    path = _session_file(agent, session_id, agent_id)
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
    """Remove the session's stored history, including every subagent slot."""
    if not agent or not session_id:
        return
    path = _session_dir(agent, session_id)
    if path is None or not path.exists():
        return
    try:
        shutil.rmtree(path)
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
