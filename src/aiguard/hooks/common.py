# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Agent-neutral helpers shared by the per-agent hook handlers.

Both the Claude Code and Codex CLI hook contracts use the same decision JSON
(``hookSpecificOutput`` / ``permissionDecision:"deny"`` / ``decision:"block"`` /
``reason``) and the same Datadog span tags, so the branded block payload, the
investigate-link builder, and the common tag scaffolding live here once and are
shared. Anything genuinely agent-specific (Claude's skill injection, each
agent's email lookup) stays in the agent's own handler.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from typing import Any

from ddtrace import tracer
from ddtrace.ext import user

from aiguard import utils
from aiguard.client import AIGuardAbortError
from aiguard.constants import AIGuardConstants

logger = logging.getLogger("ai_guard")

# Branded TUI banner shown to the user when a call is blocked.
BLOCK_BANNER = "\x1b[1;31m🛡️ Datadog AI Guard\x1b[0m Blocked by security policy"

# Sites where the UI lives at ``app.<site>``. Regional sites
# (``us3.datadoghq.com``, ``us5.datadoghq.com``, ``ap1.datadoghq.com``, …) already
# carry their subdomain and are reached at ``https://<site>`` directly — adding
# ``app.`` breaks them.
_APP_PREFIX_SITES = frozenset(
    {
        "datadoghq.com",
        "datadoghq.eu",
        "ddog-gov.com",
        "datad0g.com",
    }
)


def ai_guard_ui_url(session_id: str) -> str | None:
    """Build the Datadog AI Guard investigate link for a session."""
    if not session_id:
        return None
    site = os.environ.get("DD_SITE") or "datadoghq.com"
    host = f"app.{site}" if site in _APP_PREFIX_SITES else site
    query = urllib.parse.quote(
        f"resource_name:ai_guard "
        f"@{AIGuardConstants.CODING_AGENT_TAG}:* "
        f"@{AIGuardConstants.SESSION_ID_TAG}:{session_id}"
    )
    return f"https://{host}/security/ai-guard/investigate?query={query}&group_by=session"


def set_common_tags(
    event: dict[str, Any], *, coding_agent: str, email: str | None = None
) -> dict[str, Any]:
    """Compute the common AI Guard span tags and apply them to the active span.

    ``coding_agent`` is the ``AIGuardConstants`` agent id (``CLAUDE_CODE`` /
    ``CODEX_CLI``); ``email`` is the agent-specific authenticated user, if any.
    """
    tags: dict[str, Any] = {AIGuardConstants.CODING_AGENT_TAG: coding_agent}

    if "model" in event:
        tags[AIGuardConstants.MODEL_TAG] = event["model"]

    if email:
        tags[user.EMAIL] = email
    user_id = utils.fetch_endpoint_id()
    tags[user.ID] = user_id
    tags[AIGuardConstants.USER_ID_TAG] = user_id
    tags[AIGuardConstants.SESSION_ID_TAG] = event.get("session_id", "")
    agent_id = event.get("agent_id", "")
    if agent_id:
        tags[AIGuardConstants.SUBAGENT_ID_TAG] = agent_id
    agent_type = event.get("agent_type", "")
    if agent_type:
        tags[AIGuardConstants.SUBAGENT_TYPE_TAG] = agent_type

    span = tracer.current_span()
    if span:
        for key, value in tags.items():
            span.set_tag(key, value)

    return tags


def blocked_tool_response(
    event: dict[str, Any],
    abort: AIGuardAbortError,
    *,
    extra_context: list[str] | None = None,
) -> dict[str, Any]:
    """Shape the agent decision payload for a blocked tool call.

    ``PreToolUse`` events return a ``permissionDecision:"deny"`` with the
    branded banner as the reason and the structured guidance as
    ``additionalContext``; post-execution events instead return
    ``decision:"block"`` (the call already ran, so the turn continues and the
    model narrates the block). ``extra_context`` lets an agent append
    tool-specific guidance (e.g. Claude's skill-load advice).
    """
    event_name = event.get("hook_event_name", "")
    tool_name = event.get("tool_name", "")
    ui_url = ai_guard_ui_url(event.get("session_id", ""))

    facts = [
        f"Datadog AI Guard blocked the `{tool_name}` tool call.",
        f"- Triggering reason: `{abort.reason}`",
    ]
    if abort.tag_probs:
        ranked = sorted(abort.tag_probs.items(), key=lambda kv: kv[1], reverse=True)
        top_tag, top_prob = ranked[0]
        breakdown = ", ".join(f"`{tag}` ({prob * 100:.0f}%)" for tag, prob in ranked)
        facts.append(f"- Most likely risk: `{top_tag}` at {top_prob * 100:.0f}% confidence")
        facts.append(f"- Risk breakdown (highest first): {breakdown}")

    if ui_url:
        facts.append(f"- Investigate in Datadog: {ui_url}")

    instructions = [
        "",
        "In your next reply, write a short user-facing message that:",
        "1. States that Datadog AI Guard blocked the call to the tool above.",
        "2. Names the most likely risk category and includes its confidence as a percentage, "
        "also include other categories if they have high probabilities."
        "3. Suggests sensible next steps (rephrase the request, review the input, inspect the "
        "affected file, or contact the user's security team).",
        "4. If a Datadog investigation link is provided above, include it in the response.",
        "Do not retry the call automatically. Do not invent details beyond what is listed above.",
    ]

    parts = facts + instructions
    if extra_context:
        parts += extra_context

    model_context = "\n".join(parts)

    hook_specific_output: dict = {
        "hookEventName": event_name,
        "additionalContext": model_context,
    }
    result: dict = {
        "hookSpecificOutput": hook_specific_output,
    }
    if event_name == "PreToolUse":
        hook_specific_output["permissionDecision"] = "deny"
        hook_specific_output["permissionDecisionReason"] = BLOCK_BANNER
    else:
        result["decision"] = "block"
        result["reason"] = BLOCK_BANNER

    return result
