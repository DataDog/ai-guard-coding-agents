# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Claude/Anthropic proxy — registers the ``proxy claude`` subcommand."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any

from ddtrace import tracer
from ddtrace.appsec.ai_guard import (
    AIGuardAbortError,
    ContentPart,
    Function,
    Message,
    Options,
    ToolCall,
    new_ai_guard_client,
)
from ddtrace.ext import user

from aiguard import paths, utils
from aiguard.constants import AIGuardConstants
from aiguard.hooks.hooks import Handler

logger = logging.getLogger("ai_guard")

# CamelCase → snake_case for the dispatched method suffix
# (``SessionStart`` → ``session_start``).
_CAMEL_TO_SNAKE = re.compile(r"(?<!^)(?=[A-Z])")

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


class ClaudeHandler(Handler):
    """Handler for the Anthropic Messages API (``/v1/messages``)."""

    def __init__(self, blocking: bool) -> None:
        self._blocking = blocking
        self._ai_guard = new_ai_guard_client(
            mode=_privacy_mode(),
            meta={"coding_agent": AIGuardConstants.CLAUDE_CODE},
        )

    def agent(self) -> str:
        return "claude"

    def handle_hook(self, hook: str, payload: bytes) -> bytes:
        """Dynamically dispatch a Claude Code hook event by name.

        ``hook`` arrives in the CamelCase shape used in ``claude-settings.json``
        (e.g. ``SessionStart``); we look up ``_session_start`` on the handler.
        """
        try:
            event = json.loads(payload) if payload and payload.strip() else {}
        except (json.JSONDecodeError, ValueError):
            logger.error("claude hook %s: invalid JSON payload", hook, exc_info=True)
            event = {}

        method_name = "_" + _CAMEL_TO_SNAKE.sub("_", hook).lower()
        method = getattr(self, method_name, None)
        if not method:
            logger.warning("claude: unhandled hook %r", hook)
            return b""

        result = method(event)
        return b"" if result is None else json.dumps(result, ensure_ascii=False).encode()

    # ── Hook handlers ─────────────────────────────────────────────────────────
    # Method name is ``_<hook_in_snake_case>``. ``@tracer.wrap`` opens the span;
    # tags are set on the active span via ``tracer.current_span()``.

    @tracer.wrap(name=AIGuardConstants.SESSION_START, resource=AIGuardConstants.HOOK_RESOURCE)
    def _session_start(self, event: dict[str, Any]) -> dict[str, Any] | None:
        _set_common_tags(event)
        return None

    @tracer.wrap(name=AIGuardConstants.SESSION_END, resource=AIGuardConstants.HOOK_RESOURCE)
    def _session_end(self, event: dict[str, Any]) -> dict[str, Any] | None:
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

    @tracer.wrap(name=AIGuardConstants.PRE_TOOL, resource=AIGuardConstants.HOOK_RESOURCE)
    def _pre_tool_use(self, event: dict[str, Any]) -> dict[str, Any] | None:
        tags = _set_common_tags(event)
        agent_id = event.get("agent_id", "")
        messages = _load_messages(event.get("transcript_path", ""), agent_id)
        tool_name = event.get("tool_name", "")
        if tool_name == "Skill":
            # inject the skill content to validate if it can be safely loaded
            skill = _fetch_skill(event)
            if skill:
                messages.append(
                    Message(
                        role="tool", tool_call_id=event.get("tool_use_id", ""), content=skill[1]
                    )
                )
        try:
            self._evaluate_messages(messages, tags)
        except AIGuardAbortError as e:
            logger.error("PreToolUse: blocked tool '%s', reason=%s", tool_name, e.reason)
            return _blocked_tool_response(event, e)

        return None

    @tracer.wrap(name=AIGuardConstants.POST_TOOL, resource=AIGuardConstants.HOOK_RESOURCE)
    def _post_tool_use(self, event: dict[str, Any]) -> dict[str, Any] | None:
        tags = _set_common_tags(event)
        agent_id = event.get("agent_id", "")
        messages = _load_messages(event.get("transcript_path", ""), agent_id)
        tool_response = event.get("tool_response", "")
        _append_tool_result(
            messages, event.get("tool_use_id", ""), _resolve_tool_content(tool_response) or ""
        )
        try:
            self._evaluate_messages(messages, tags)
        except AIGuardAbortError as e:
            return _blocked_tool_response(event, e)

        return None

    @tracer.wrap(name=AIGuardConstants.POST_TOOL, resource=AIGuardConstants.HOOK_RESOURCE)
    def _post_tool_use_failure(self, event: dict[str, Any]) -> dict[str, Any] | None:
        tags = _set_common_tags(event)
        agent_id = event.get("agent_id", "")
        messages = _load_messages(event.get("transcript_path", ""), agent_id)
        _append_tool_result(messages, event.get("tool_use_id", ""), event.get("error", ""))

        try:
            self._evaluate_messages(messages, tags)
        except AIGuardAbortError as e:
            tool_name = event.get("tool_name", "")
            logger.error("PostToolUseFailure: blocked tool '%s', reason=%s", tool_name, e.reason)
            return _blocked_tool_response(event, e)

        return None

    def _evaluate_messages(self, messages: list[Message], tags: dict[str, Any]) -> None:
        if messages:
            try:
                self._ai_guard.evaluate(messages, Options(block=self._blocking, tags=tags))
            except AIGuardAbortError:
                raise
            except Exception:
                logger.error("message evaluation with AI Guard failed", exc_info=True)
        return None


# ── Helpers ──────────────────────────────────────────────────────────────


def _privacy_mode() -> str:
    """Resolve ``DD_AI_GUARD_PRIVACY_MODE`` for the AI Guard client ``mode``.

    CODING_AGENT (our default) surfaces full message contents in the UI only for
    failed evaluations — on an allowed call the tool arguments and results are
    stripped. DEFAULT surfaces full contents for every evaluation. Unknown
    values fall back to CODING_AGENT.
    """
    value = (os.environ.get(AIGuardConstants.PRIVACY_MODE_ENV) or "").strip().upper()
    if value == AIGuardConstants.PRIVACY_MODE_DEFAULT:
        return AIGuardConstants.PRIVACY_MODE_DEFAULT
    return AIGuardConstants.PRIVACY_MODE_CODING_AGENT


def _load_messages(transcript_path: str, agent_id: str) -> list[Message]:
    """Rebuild the conversation history from Claude Code's session transcript."""
    path = _resolve_transcript(transcript_path, agent_id)
    if path is None:
        return []

    messages: list[Message] = []
    for entry in _read_transcript(path):
        messages.extend(_entry_to_messages(entry))
    return messages


def _append_tool_result(
    messages: list[Message], tool_use_id: str, content: str | list[ContentPart]
) -> None:
    """Append a tool-result message unless the transcript already carries it.

    Depending on flush timing, the transcript loaded for a ``PostToolUse`` event
    may already include the result of the call we are evaluating. Re-appending it
    from the hook payload would duplicate the message, so skip when a tool message
    for ``tool_use_id`` is already present.
    """
    if tool_use_id and any(
        m.get("role") == "tool" and m.get("tool_call_id") == tool_use_id for m in messages
    ):
        return
    messages.append(Message(role="tool", tool_call_id=tool_use_id, content=content))


def _resolve_transcript(transcript_path: str, agent_id: str) -> Path | None:
    """Return the transcript file for ``agent_id`` (or the main session)."""
    if not transcript_path:
        return None
    try:
        main = Path(transcript_path).expanduser()
    except (OSError, ValueError):
        return None

    if agent_id:
        # Subagent transcripts sit under "<session>/subagents/agent-<id>.jsonl",
        # alongside the main "<session>.jsonl" file.
        subagent = main.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"
        if subagent.is_file():
            return subagent

    return main if main.is_file() else None


def _read_transcript(path: Path) -> list[dict[str, Any]]:
    """Parse a JSONL transcript, skipping blank or malformed lines."""
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
                    logger.debug("transcript %s: skipping malformed line", path)
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
    except OSError:
        logger.error("failed to read transcript %s", path, exc_info=True)
    return entries


def _entry_to_messages(entry: dict[str, Any]) -> list[Message]:
    """Translate one transcript entry into zero or more AI Guard messages.

    Only ``user`` and ``assistant`` turns carry conversation content; metadata
    rows (mode changes, snapshots, summaries, …) are ignored. A single turn may
    expand into several messages: Anthropic packs tool results into a ``user``
    turn and tool calls into an ``assistant`` turn, both of which AI Guard
    models as standalone messages.
    """
    if entry.get("type") not in ("user", "assistant"):
        return []
    message = entry.get("message")
    if not isinstance(message, dict):
        return []

    role = message.get("role")
    content = message.get("content")

    if isinstance(content, str):
        return [Message(role=role, content=content)] if content else []
    if not isinstance(content, list):
        return []

    if role == "user":
        return _user_blocks_to_messages(content)
    if role == "assistant":
        return _assistant_blocks_to_messages(content)
    return []


def _user_blocks_to_messages(blocks: list) -> list[Message]:
    """Split a user turn into tool-result messages plus any plain content."""
    messages: list[Message] = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "tool_result":
            messages.append(
                Message(
                    role="tool",
                    tool_call_id=b.get("tool_use_id", ""),
                    content=_resolve_tool_content(b.get("content")),
                )
            )
    parts = _to_content_parts(blocks, exclude_types={"tool_result"})
    if parts:
        messages.append(Message(role="user", content=parts))
    return messages


def _assistant_blocks_to_messages(blocks: list) -> list[Message]:
    """Fold an assistant turn into a single message with text and tool calls."""
    tool_calls: list[ToolCall] = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "tool_use":
            try:
                arguments = json.dumps(b.get("input", {}), ensure_ascii=False)
            except (TypeError, ValueError):
                arguments = "{}"
            tool_calls.append(
                ToolCall(
                    id=b.get("id", ""),
                    function=Function(name=b.get("name", ""), arguments=arguments),
                )
            )

    parts = _to_content_parts(blocks, exclude_types={"tool_use"})
    if not parts and not tool_calls:
        return []

    message: Message = {"role": "assistant"}
    if parts:
        message["content"] = parts
    if tool_calls:
        message["tool_calls"] = tool_calls
    return [message]


def _to_content_part(block: object) -> ContentPart | None:
    """Convert any Anthropic content block to a ContentPart, preserving type.

    - text blocks: keep raw text
    - other blocks (image, tool_use, tool_result, thinking, etc.): serialize as JSON
    """
    if not isinstance(block, dict):
        try:
            return ContentPart(type="unknown", text=json.dumps(block, ensure_ascii=False))
        except Exception:
            return None

    btype = block.get("type") or "unknown"
    if btype == "text":
        text = block.get("text", "")
        return ContentPart(type="text", text=text) if text else None

    try:
        payload = json.dumps(block, ensure_ascii=False)
    except Exception:
        payload = str(block)
    return ContentPart(type=btype, text=payload)


def _to_content_parts(blocks: list, *, exclude_types: set[str] | None = None) -> list[ContentPart]:
    """Convert a list of blocks to ContentParts, optionally excluding certain types."""
    parts: list[ContentPart] = []
    for b in blocks or []:
        if isinstance(b, dict) and exclude_types and (b.get("type") in exclude_types):
            continue
        cp = _to_content_part(b)
        if cp is not None:
            parts.append(cp)
    return parts


def _resolve_tool_content(raw) -> str | list[ContentPart]:
    if isinstance(raw, list):
        parts = _to_content_parts(raw)
        return parts or []
    # For non-list content, pass through string content as-is; otherwise JSON-serialize
    if isinstance(raw, str) or raw is None:
        return raw or ""
    try:
        return json.dumps(raw, ensure_ascii=False)
    except Exception:
        return str(raw)


def _ai_guard_ui_url(session_id: str) -> str | None:
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


def _fetch_email() -> str | None:
    """Return the email of the authenticated Claude Code user from ~/.claude.json."""
    try:
        data = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
        email = data.get("oauthAccount", {}).get("emailAddress")
        if email:
            return email
    except (OSError, json.JSONDecodeError, AttributeError):
        logger.debug("failed to read ~/.claude.json", exc_info=True)
    return None


def _set_common_tags(event: dict[str, Any]) -> dict[str, Any]:
    tags: dict[str, Any] = {
        AIGuardConstants.CODING_AGENT_TAG: AIGuardConstants.CLAUDE_CODE,
    }

    if "model" in event:
        tags[AIGuardConstants.MODEL_TAG] = event["model"]

    email = _fetch_email()
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


def _blocked_tool_response(event: dict[str, Any], abort: AIGuardAbortError) -> dict[str, Any]:
    event_name = event.get("hook_event_name", "")
    tool_name = event.get("tool_name", "")
    ui_url = _ai_guard_ui_url(event.get("session_id", ""))
    display_reason = "\x1b[1;31m🛡️ Datadog AI Guard\x1b[0m Blocked by security policy"

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

    if tool_name == "Skill":
        skill_folder = _skill_folder(event)
        location = f" located at `{skill_folder}`" if skill_folder else ""
        parts += [
            "",
            f"The blocked call was a skill load{location}. Also tell the user to remove this skill "
            "and audit any other recently installed skills. Do not delete the skill yourself, and "
            "do not attempt to load it again in this session.",
        ]

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
        hook_specific_output["permissionDecisionReason"] = display_reason
    else:
        result["decision"] = "block"
        result["reason"] = display_reason

    return result


def _skill_folder(event: dict[str, Any]) -> Path | None:
    tool_input = event.get("tool_input", {})
    skill = tool_input.get("skill", None)
    cwd = event.get("cwd", "")
    return _find_skill_folder(cwd, skill)


def _fetch_skill(event: dict[str, Any]) -> tuple[Path, str] | None:
    skill_folder = _skill_folder(event)
    if not skill_folder:
        return None
    md = skill_folder / "SKILL.md"
    try:
        return md, md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.error("failed to read skill %s", md, exc_info=True)
        return None


def _find_skill_folder(cwd: str, skill: str) -> Path | None:
    """Locate a Claude Code skill folder by name.

    A skill folder is identified by a ``SKILL.md`` marker file. Names may be
    plain (``"foo"``) or namespaced (``"plugin:foo"`` — the prefix scopes the
    search to that plugin first). All known install locations are checked, in
    precedence order:

      1. Project-level: ``<cwd>/.claude/skills/<name>`` and the same path on
         every ancestor up to the filesystem root (so a skill installed at
         the repo root is found from a deeply nested ``cwd``).
      2. User-level: ``<claude_config_dir>/skills/<name>`` (``~/.claude`` by
         default, or ``$CLAUDE_CONFIG_DIR`` when set).
      3. Plugin-scoped (when the ``"plugin:"`` prefix is present): any
         ``<claude_config_dir>/plugins/**/skills/<name>`` whose path contains
         the plugin segment.
      4. Fallback: any ``<claude_config_dir>/plugins/**/skills/<name>``.

    Returns the first existing folder containing ``SKILL.md``, or ``None``.
    """
    if not skill:
        return None

    plugin, sep, suffix = skill.partition(":")
    if sep:
        name = suffix
    else:
        name, plugin = plugin, ""
    if not name:
        return None

    seen: set[Path] = set()
    candidates: list[Path] = []

    def _add(skills_root: Path, leaf: str) -> None:
        try:
            root = skills_root.resolve(strict=False)
            target = (skills_root / leaf).resolve(strict=False)
            target.relative_to(root)
        except (OSError, ValueError):
            return  # invalid path components or escapes the skills root
        if target in seen:
            return
        seen.add(target)
        candidates.append(target)

    # 1. Project-level: walk up from cwd until the filesystem root.
    if cwd:
        try:
            here = Path(cwd).expanduser().resolve(strict=False)
            for parent in (here, *here.parents):
                _add(parent / ".claude" / "skills", name)
        except (OSError, ValueError):
            logger.debug("find_skill: invalid cwd %r", cwd)

    # 2. User-level skills directory.
    claude_home = paths.claude_config_dir()
    _add(claude_home / "skills", name)

    # 3 & 4. Plugin marketplaces — plugin-scoped first, then any.
    plugins_root = claude_home / "plugins"
    if plugins_root.is_dir():
        try:
            skills_dirs = [d for d in plugins_root.rglob("skills") if d.is_dir()]
        except OSError:
            logger.debug("find_skill: error walking %s", plugins_root, exc_info=True)
            skills_dirs = []
        if plugin:
            for d in skills_dirs:
                if plugin in d.parts:
                    _add(d, name)
        for d in skills_dirs:
            _add(d, name)

    for candidate in candidates:
        try:
            if (candidate / "SKILL.md").is_file():
                return candidate
        except OSError:
            continue

    logger.debug("find_skill: %r not found (checked %d locations)", skill, len(candidates))
    return None
