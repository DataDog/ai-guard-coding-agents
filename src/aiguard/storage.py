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
from pathlib import Path

from ddtrace.appsec.ai_guard import Message

logger = logging.getLogger("ai_guard")


def _storage_root() -> Path:
    """Return the on-disk root for AI Guard state. Honors ``DD_AI_GUARD_HOME``."""
    return Path(os.environ.get("DD_AI_GUARD_HOME") or (Path.home() / ".ai_guard"))


def _session_file(agent: str, session_id: str) -> Path | None:
    """Return the absolute path of a session's JSON file.

    ``None`` if the resolved path would escape the storage root (defense against
    path traversal via attacker-controlled ``agent``/``session_id``).
    """
    try:
        root = _storage_root().resolve(strict=False)
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
