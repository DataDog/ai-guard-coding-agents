# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Codex CLI hook handler.

Codex's hook contract mirrors Claude Code's: each lifecycle event invokes
``ai-guard hook codex <Event>`` with the event JSON on stdin, and a decision is
written to stdout (empty body to allow, a ``permissionDecision:"deny"`` /
``decision:"block"`` payload to stop the call). The conversation is rebuilt from
Codex's rollout transcript (OpenAI Responses API items — see
:mod:`aiguard.codex.translate`).

Compared with Claude there is no slash-command expansion, no skill tool, and no
separate subagent transcript file, so the handler is a leaner version of
:class:`aiguard.claude.handler.ClaudeHandler`.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from pathlib import Path
from typing import Any

from ddtrace import tracer

from aiguard import paths
from aiguard.client import (
    AIGuardAbortError,
    ContentPart,
    Message,
    Options,
    new_ai_guard_client,
)
from aiguard.codex import translate
from aiguard.constants import AIGuardConstants
from aiguard.hooks import common
from aiguard.hooks.hooks import Handler

logger = logging.getLogger("ai_guard")

# CamelCase → snake_case for the dispatched method suffix
# (``PreToolUse`` → ``pre_tool_use``).
_CAMEL_TO_SNAKE = re.compile(r"(?<!^)(?=[A-Z])")

# Explicit skill mention in a prompt: ``$skill-name`` (leading letter required so
# positional args like ``$1`` and shell vars aren't treated as skills).
_SKILL_REF = re.compile(r"(?:^|\s)\$([A-Za-z][A-Za-z0-9._-]*)")


class CodexHandler(Handler):
    """Handler for OpenAI Codex CLI hook events."""

    def __init__(self, blocking: bool) -> None:
        self._blocking = blocking
        self._ai_guard = new_ai_guard_client(
            meta={"coding_agent": AIGuardConstants.CODEX_CLI},
        )

    def agent(self) -> str:
        return "codex"

    def handle_hook(self, hook: str, payload: bytes) -> bytes:
        """Dynamically dispatch a Codex hook event by name.

        ``hook`` arrives CamelCased (e.g. ``PreToolUse``); we look up
        ``_pre_tool_use`` on the handler.
        """
        try:
            event = json.loads(payload) if payload and payload.strip() else {}
        except (json.JSONDecodeError, ValueError):
            logger.error("codex hook %s: invalid JSON payload", hook, exc_info=True)
            event = {}

        method_name = "_" + _CAMEL_TO_SNAKE.sub("_", hook).lower()
        method = getattr(self, method_name, None)
        if not method:
            logger.error("codex: unhandled hook %r", hook)
            return b""

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "codex: dispatching hook %s: %s",
                hook,
                json.dumps(event, ensure_ascii=False, default=str),
            )
        result = method(event)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "codex: hook %s -> %s",
                hook,
                json.dumps(result, ensure_ascii=False, default=str) if result else "allow",
            )
        return b"" if result is None else json.dumps(result, ensure_ascii=False).encode()

    # ── Hook handlers ─────────────────────────────────────────────────────────

    @tracer.wrap(name=AIGuardConstants.SESSION_START, resource=AIGuardConstants.HOOK_RESOURCE)
    def _session_start(self, event: dict[str, Any]) -> dict[str, Any] | None:
        _set_common_tags(event)
        return None

    @tracer.wrap(name=AIGuardConstants.SUBAGENT_START, resource=AIGuardConstants.HOOK_RESOURCE)
    def _subagent_start(self, event: dict[str, Any]) -> dict[str, Any] | None:
        _set_common_tags(event)
        return None

    @tracer.wrap(name=AIGuardConstants.SUBAGENT_STOP, resource=AIGuardConstants.HOOK_RESOURCE)
    def _subagent_stop(self, event: dict[str, Any]) -> dict[str, Any] | None:
        _set_common_tags(event)
        return None

    @tracer.wrap(name=AIGuardConstants.STOP, resource=AIGuardConstants.HOOK_RESOURCE)
    def _stop(self, event: dict[str, Any]) -> dict[str, Any] | None:
        # Codex has no SessionEnd; ``Stop`` fires at the end of each turn. We
        # only emit a span (there is no stored history to clear).
        _set_common_tags(event)
        return None

    @tracer.wrap(name=AIGuardConstants.PRE_TOOL, resource=AIGuardConstants.HOOK_RESOURCE)
    def _pre_tool_use(self, event: dict[str, Any]) -> dict[str, Any] | None:
        tags = _set_common_tags(event)
        messages = _load_messages(event.get("transcript_path"))
        tool_name = event.get("tool_name", "")
        # Evaluate the pending call itself — the rollout likely has not flushed
        # it yet (the call hasn't executed).
        _append_pending_tool_call(messages, event)
        try:
            self._evaluate_messages(messages, tags)
        except AIGuardAbortError as e:
            logger.error("PreToolUse: blocked tool '%s', reason=%s", tool_name, e.reason)
            return common.blocked_tool_response(event, e)

        return None

    @tracer.wrap(name=AIGuardConstants.POST_TOOL, resource=AIGuardConstants.HOOK_RESOURCE)
    def _post_tool_use(self, event: dict[str, Any]) -> dict[str, Any] | None:
        # Codex fires PostToolUse on both success and non-zero exit, so this one
        # method covers what Claude splits into PostToolUse / PostToolUseFailure.
        tags = _set_common_tags(event)
        messages = _load_messages(event.get("transcript_path"))
        _append_tool_result(
            messages,
            event.get("tool_use_id", ""),
            translate.resolve_tool_content(_tool_output(event)),
        )
        try:
            self._evaluate_messages(messages, tags)
        except AIGuardAbortError as e:
            logger.error(
                "PostToolUse: blocked tool '%s', reason=%s", event.get("tool_name", ""), e.reason
            )
            return common.blocked_tool_response(event, e)

        return None

    @tracer.wrap(name=AIGuardConstants.USER_PROMPT_SUBMIT, resource=AIGuardConstants.HOOK_RESOURCE)
    def _user_prompt_submit(self, event: dict[str, Any]) -> dict[str, Any] | None:
        # Codex's analog of Claude's UserPromptExpansion. We evaluate the prompt
        # itself and, for explicit ``$skill`` / ``/prompts:`` references, resolve
        # and inject the definition so AI Guard inspects what the expansion will
        # load — not just the line the user typed.
        tags = _set_common_tags(event)
        messages = _load_messages(event.get("transcript_path"))
        prompt = event.get("prompt", "")
        if prompt:
            messages.append(Message(role="user", content=prompt))
        messages.extend(_fetch_prompt_expansion(event))
        try:
            self._evaluate_messages(messages, tags)
        except AIGuardAbortError as e:
            logger.error("UserPromptSubmit: blocked prompt, reason=%s", e.reason)
            return _blocked_prompt_response(event, e)

        return None

    def _evaluate_messages(self, messages: list[Message], tags: dict[str, Any]) -> None:
        if messages:
            logger.debug(
                "evaluating %d message(s) with AI Guard (block=%s)", len(messages), self._blocking
            )
            try:
                self._ai_guard.evaluate(messages, Options(block=self._blocking, tags=tags))
            except AIGuardAbortError:
                raise
            except Exception:
                logger.error("message evaluation with AI Guard failed", exc_info=True)
        else:
            logger.debug("no messages to evaluate; skipping AI Guard call")
        return None


# ── Helpers ──────────────────────────────────────────────────────────────


def _tool_output(event: dict[str, Any]) -> Any:
    """Pull the tool output from a Codex PostToolUse event.

    Codex names the field ``tool_response`` (mirroring Claude); fall back to
    ``tool_output`` / ``output`` for resilience to naming drift.
    """
    for key in ("tool_response", "tool_output", "output"):
        if key in event:
            return event[key]
    return ""


def _load_messages(transcript_path: str | None) -> list[Message]:
    """Rebuild the conversation history from Codex's rollout transcript."""
    if not transcript_path:
        logger.debug("codex: no transcript_path on event; evaluating pending call only")
        return []
    try:
        path = Path(transcript_path).expanduser()
    except (OSError, ValueError):
        return []
    if not path.is_file():
        logger.debug("codex: transcript %s is not a file", path)
        return []

    entries = _read_transcript(path)
    messages = translate.transcript_to_messages(entries)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "codex transcript %s: read %d entr(y/ies), parsed %d message(s)",
            path,
            len(entries),
            len(messages),
        )
    return messages


def _read_transcript(path: Path) -> list[dict[str, Any]]:
    """Parse a rollout JSONL transcript, skipping blank or malformed lines."""
    entries: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    logger.debug("codex transcript %s: skipping malformed line", path)
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
    except OSError:
        logger.error("failed to read codex transcript %s", path, exc_info=True)
    return entries


def _append_tool_result(
    messages: list[Message], tool_use_id: str, content: str | list[ContentPart]
) -> None:
    """Append a tool-result message unless the transcript already carries it."""
    if tool_use_id and any(
        m.get("role") == "tool" and m.get("tool_call_id") == tool_use_id for m in messages
    ):
        return
    messages.append(Message(role="tool", tool_call_id=tool_use_id, content=content))


def _append_pending_tool_call(messages: list[Message], event: dict[str, Any]) -> None:
    """Ensure the PreToolUse pending tool call is part of what we evaluate."""
    tool_name = event.get("tool_name", "")
    if not tool_name:
        return
    tool_use_id = event.get("tool_use_id", "")
    # Build the call the same way a rollout ``function_call`` would translate, so
    # the dedup below matches an already-flushed call. ``tool_input`` is a parsed
    # object here; ``function_call_to_call`` serialises it to the arguments string.
    call = translate.function_call_to_call(
        {"call_id": tool_use_id, "name": tool_name, "arguments": event.get("tool_input", {})}
    )
    arguments = call["function"]["arguments"]

    for message in messages:
        if message.get("role") != "assistant":
            continue
        for existing in message.get("tool_calls", []) or []:
            if tool_use_id and existing.get("id") == tool_use_id:
                return
            function = existing.get("function", {})
            if function.get("name") == tool_name and function.get("arguments") == arguments:
                return

    messages.append(Message(role="assistant", tool_calls=[call]))


def _set_common_tags(event: dict[str, Any]) -> dict[str, Any]:
    return common.set_common_tags(
        event, coding_agent=AIGuardConstants.CODEX_CLI, email=_fetch_email()
    )


def _fetch_email() -> str | None:
    """Return the email of the authenticated Codex user (best effort).

    ChatGPT-authenticated Codex stores an OIDC ``id_token`` in
    ``$CODEX_HOME/auth.json`` whose payload carries an ``email`` claim. We decode
    the JWT payload without verifying the signature — it is used only as a span
    tag, never for trust — and tolerate every failure mode (API-key auth with no
    token, malformed file, missing claim).
    """
    try:
        auth = json.loads((paths.codex_config_dir() / "auth.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.debug("codex: failed to read auth.json", exc_info=True)
        return None

    token = (auth.get("tokens") or {}).get("id_token")
    if not isinstance(token, str) or token.count(".") < 2:
        return None

    payload_segment = token.split(".")[1]
    payload_segment += "=" * (-len(payload_segment) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload_segment))
    except (binascii.Error, ValueError, json.JSONDecodeError):
        logger.debug("codex: failed to decode id_token payload", exc_info=True)
        return None

    email = claims.get("email")
    return email if isinstance(email, str) and email else None


# ── Prompt expansion (skills / custom prompts) ─────────────────────────────


def _fetch_prompt_expansion(event: dict[str, Any]) -> list[Message]:
    """Resolve explicit ``$skill`` / ``/prompts:`` references in a submitted prompt.

    Each resolved definition is modelled as an assistant tool call plus its
    result (mirroring Claude's ``UserPromptExpansion`` handling), so AI Guard
    inspects the instructions the expansion will inject. Unresolved references
    inject nothing — Codex may already have expanded the prompt in place, in
    which case the content is evaluated directly via the prompt message.
    """
    prompt = event.get("prompt", "")
    if not prompt:
        return []
    cwd = event.get("cwd", "")
    messages: list[Message] = []

    seen: set[str] = set()
    for name in _SKILL_REF.findall(prompt):
        if name in seen:
            continue
        seen.add(name)
        content = _fetch_codex_skill(cwd, name)
        if content is not None:
            messages.extend(_modelled_expansion("skill", name, content))

    # Slash-command custom prompts are the first token of the line: ``/name`` or
    # ``/prompts:name`` → ``~/.codex/prompts/name.md``.
    stripped = prompt.strip()
    if stripped.startswith("/") and len(stripped) > 1:
        token = stripped[1:].split()[0]
        name = token.split(":")[-1]
        content = _find_codex_prompt(name) if name else None
        if content is not None:
            messages.extend(_modelled_expansion("command", name, content))

    return messages


def _modelled_expansion(kind: str, name: str, content: str) -> list[Message]:
    """Model a resolved skill/command as a tool call plus its result."""
    tool_use_id = f"expansion-{name}"
    call = translate.function_call_to_call(
        {"call_id": tool_use_id, "name": kind, "arguments": {"name": name}}
    )
    return [
        Message(role="assistant", tool_calls=[call]),
        Message(role="tool", tool_call_id=tool_use_id, content=content),
    ]


def _codex_skills_roots(cwd: str) -> list[Path]:
    """Candidate skill roots, in precedence order (see Codex skills docs)."""
    roots: list[Path] = []
    if cwd:
        try:
            here = Path(cwd).expanduser()
            for parent in (here, *here.parents):
                roots.append(parent / ".agents" / "skills")
        except (OSError, ValueError):
            logger.debug("codex: invalid cwd %r for skill lookup", cwd)
    roots.append(Path.home() / ".agents" / "skills")
    # Some Codex versions / community guides also scan ``$CODEX_HOME/skills``.
    roots.append(paths.codex_config_dir() / "skills")
    return roots


def _fetch_codex_skill(cwd: str, name: str) -> str | None:
    """Return the ``SKILL.md`` body for ``$name``, or ``None`` if not found."""
    if not name:
        return None
    for root in _codex_skills_roots(cwd):
        md = root / name / "SKILL.md"
        try:
            if md.is_file():
                return md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.debug("codex: failed to read skill %s", md, exc_info=True)
    return None


def _find_codex_prompt(name: str) -> str | None:
    """Return a custom-prompt markdown body from ``$CODEX_HOME/prompts``."""
    md = paths.codex_config_dir() / "prompts" / f"{name}.md"
    try:
        if md.is_file():
            return md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.debug("codex: failed to read prompt %s", md, exc_info=True)
    return None


def _blocked_prompt_response(event: dict[str, Any], abort: AIGuardAbortError) -> dict[str, Any]:
    """Block a submitted prompt (``UserPromptSubmit``).

    The prompt is erased and no model turn follows, so guidance can't be routed
    to the model via ``additionalContext``; the explanation goes in ``reason``,
    which Codex surfaces directly to the user.
    """
    lines = [
        "\x1b[1;31m🛡️ Datadog AI Guard\x1b[0m blocked your prompt by security policy.",
        f"Reason: {abort.reason}",
    ]
    if abort.tag_probs:
        ranked = sorted(abort.tag_probs.items(), key=lambda kv: kv[1], reverse=True)
        top_tag, top_prob = ranked[0]
        lines.append(f"Most likely risk: {top_tag} at {top_prob * 100:.0f}% confidence")
        high = [f"{tag} ({prob * 100:.0f}%)" for tag, prob in ranked if prob >= 0.5]
        if len(high) > 1:
            lines.append(f"Other high-confidence risks: {', '.join(high[1:])}")

    ui_url = common.ai_guard_ui_url(event.get("session_id", ""))
    if ui_url:
        lines.append(f"Investigate in Datadog: {ui_url}")

    return {
        "decision": "block",
        "reason": "\n".join(lines),
        "hookSpecificOutput": {
            "hookEventName": event.get("hook_event_name", ""),
        },
    }
