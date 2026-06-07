# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""``ai-guard hook AGENT HOOK``"""

from __future__ import annotations

import logging
import os
import sys
from abc import ABC, abstractmethod

import click

logger = logging.getLogger("ai_guard")

_TRUTHY = frozenset({"1", "true", "t", "yes", "y", "on"})


def _resolve_block() -> bool:
    return os.environ.get("DD_AI_GUARD_BLOCK", "true").strip().lower() in _TRUTHY


class Handler(ABC):
    """Handles hook events emitted by a specific coding agent."""

    @abstractmethod
    def agent(self) -> str:
        """Return the name of the agent."""

    @abstractmethod
    def handle_hook(self, hook: str, body: bytes) -> bytes:
        """Dispatch the named hook event; return the agent-shaped response body."""


def _build_handler(agent: str, block: bool) -> Handler | None:
    """Instantiate the :class:`Handler` registered for ``agent``.

    Handlers are imported lazily: each pulls in ddtrace and the AI Guard client,
    which we don't want to load for unrelated CLI commands.
    """
    if agent == "claude":
        from aiguard.claude.handler import ClaudeHandler

        return ClaudeHandler(block)

    return None


@click.command("hook")
@click.argument("agent")
@click.argument("hook_name", metavar="HOOK")
def hook(agent: str, hook_name: str) -> None:
    """Dispatch a hook event to the handler for the selected agent.

    Reads the event payload from stdin, hands it to the agent's
    :class:`Handler`, and writes any response body to stdout. The ``DD_*`` keys
    the handler needs are loaded into the environment from ``config.env`` by the
    top-level CLI before this command runs.

    A failed hook must never break the host agent's command flow, so every
    error is logged and swallowed (the agent sees an empty response and
    proceeds as if no hook ran).

    \b
    Examples:
      ai-guard hook claude SessionStart < event.json
      ai-guard hook claude SubagentStop < event.json
    """
    try:
        payload = sys.stdin.buffer.read()

        handler = _build_handler(agent, _resolve_block())
        if handler is None:
            logger.error("no hook handler registered for agent %r", agent)
            return

        response = handler.handle_hook(hook_name, payload)

        if response:
            sys.stdout.buffer.write(response)
    except Exception:
        # Swallowed by design: a failed hook must not break the host agent's
        # command flow. The error is logged with traceback for diagnosis.
        logger.exception("failed to invoke hook %r for agent %r", hook_name, agent)
