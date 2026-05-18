"""Utilities for identifying the Claude Code user."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("ai_guard")


def fetch_user_id() -> str | None:
    """Return the email of the authenticated Claude Code user from ~/.claude.json."""
    try:
        data = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
        email = data.get("oauthAccount", {}).get("emailAddress")
        if email:
            return email
    except (OSError, json.JSONDecodeError, AttributeError):
        logger.debug("failed to read ~/.claude.json", exc_info=True)
    return None
