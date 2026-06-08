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
from ddtrace.ext import user

from aiguard import paths, utils
from aiguard.claude import translate
from aiguard.client import (
    AIGuardAbortError,
    ContentPart,
    Message,
    Options,
    new_ai_guard_client,
)
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
            logger.error("claude: unhandled hook %r", hook)
            return b""

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "claude: dispatching hook %s: %s",
                hook,
                json.dumps(event, ensure_ascii=False, default=str),
            )
        result = method(event)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "claude: hook %s -> %s",
                hook,
                json.dumps(result, ensure_ascii=False, default=str) if result else "allow",
            )
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
        # Evaluate the pending call itself — never rely on the transcript having
        # flushed it (it may not have, e.g. the first tool call in a session).
        _append_pending_tool_call(messages, event)
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
            messages,
            event.get("tool_use_id", ""),
            translate.resolve_tool_content(tool_response) or "",
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

    @tracer.wrap(
        name=AIGuardConstants.USER_PROMPT_EXPANSION, resource=AIGuardConstants.HOOK_RESOURCE
    )
    def _user_prompt_expansion(self, event: dict[str, Any]) -> dict[str, Any] | None:
        tags = _set_common_tags(event)
        agent_id = event.get("agent_id", "")
        messages = _load_messages(event.get("transcript_path", ""), agent_id)
        prompt = event.get("prompt", "")
        if prompt:
            messages.append(Message(role="user", content=prompt))
        # Inject the command/skill definition so AI Guard inspects what the
        # expansion will actually load, not just the line the user typed.
        expansion = _fetch_command_expansion(event)
        if expansion:
            messages.extend(expansion)
        try:
            self._evaluate_messages(messages, tags)
        except AIGuardAbortError as e:
            logger.error(
                "UserPromptExpansion: blocked command '%s', reason=%s",
                event.get("command_name", ""),
                e.reason,
            )
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


def _load_messages(transcript_path: str, agent_id: str) -> list[Message]:
    """Rebuild the conversation history from Claude Code's session transcript."""
    path = _resolve_transcript(transcript_path, agent_id)
    if path is None:
        logger.debug(
            "no transcript resolved (transcript_path=%r, agent_id=%r)", transcript_path, agent_id
        )
        return []

    entries = _read_transcript(path)
    messages = translate.transcript_to_messages(entries)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "transcript %s: read %d entr(y/ies): %s",
            path,
            len(entries),
            json.dumps(entries, ensure_ascii=False, default=str),
        )
        logger.debug(
            "transcript %s: parsed %d message(s): %s",
            path,
            len(messages),
            json.dumps(messages, ensure_ascii=False, default=str),
        )
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


def _append_pending_tool_call(messages: list[Message], event: dict[str, Any]) -> None:
    """Ensure the PreToolUse pending tool call is part of what we evaluate."""
    tool_name = event.get("tool_name", "")
    if not tool_name:
        return
    tool_use_id = event.get("tool_use_id", "")
    # Build the call the same way a transcript ``tool_use`` block would be
    # translated, so the dedup comparison below matches an already-flushed call.
    call = translate.tool_use_to_call(
        {"id": tool_use_id, "name": tool_name, "input": event.get("tool_input", {})}
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


def _blocked_prompt_response(event: dict[str, Any], abort: AIGuardAbortError) -> dict[str, Any]:
    """Block a slash command / skill expansion (``UserPromptExpansion``).

    Unlike a blocked tool call — where the turn continues and Claude can narrate
    the block to the user — ``decision: "block"`` here erases the command from
    context and no model turn follows. So there is no point routing guidance to
    the model via ``additionalContext``; the explanation must go in ``reason``,
    which Claude Code surfaces directly to the user. We pack the command name,
    the top risk + confidence, and the investigate link into that reason.
    """
    command = event.get("command_name") or event.get("command", "")
    target = f"`/{command}`" if command else "this command"
    lines = [
        f"\x1b[1;31m🛡️ Datadog AI Guard\x1b[0m blocked {target} by security policy.",
        f"Reason: {abort.reason}",
    ]
    if abort.tag_probs:
        ranked = sorted(abort.tag_probs.items(), key=lambda kv: kv[1], reverse=True)
        top_tag, top_prob = ranked[0]
        lines.append(f"Most likely risk: {top_tag} at {top_prob * 100:.0f}% confidence")
        high = [f"{tag} ({prob * 100:.0f}%)" for tag, prob in ranked if prob >= 0.5]
        if len(high) > 1:
            lines.append(f"Other high-confidence risks: {', '.join(high[1:])}")

    ui_url = _ai_guard_ui_url(event.get("session_id", ""))
    if ui_url:
        lines.append(f"Investigate in Datadog: {ui_url}")

    return {
        "decision": "block",
        "reason": "\n".join(lines),
        "hookSpecificOutput": {
            "hookEventName": event.get("hook_event_name", ""),
        },
    }


def _fetch_command_expansion(event: dict[str, Any]) -> list[Message]:
    """Model a slash command / skill expansion as a tool call plus its result.

    ``UserPromptExpansion`` only carries the command name and the raw line the
    user typed — not what it expands into. We resolve the definition ourselves
    (a slash command's markdown file, or a skill's ``SKILL.md``) and present it
    to AI Guard as an assistant ``command``/``skill`` tool call followed by a
    ``tool`` message holding the expanded body, so the instructions about to be
    injected are evaluated the same way a real tool invocation would be.

    Returns an empty list when no definition can be located.
    """
    expansion_type = event.get("expansion_type", "")
    if expansion_type != "slash_command":
        return []

    command = event.get("command_name", "")
    if not command:
        logger.debug("expansion: no command_name in event; nothing to inject")
        return []

    cwd = event.get("cwd", "")
    command_args = event.get("command_args", "")
    logger.debug(
        "expansion: resolving command_name=%r type=%r (cwd=%r)", command, expansion_type, cwd
    )

    kind, content = "command", None
    command_file = _find_command_file(cwd, command)
    if command_file is not None:
        logger.debug("expansion: resolved command %r to file %s", command, command_file)
        try:
            content = command_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.error("failed to read command %s", command_file, exc_info=True)
    else:
        logger.debug("expansion: no slash-command file found for %r", command)

    if content is None:
        skill_folder = _find_skill_folder(cwd, command)
        if skill_folder is not None:
            md = skill_folder / "SKILL.md"
            logger.debug("expansion: resolved command %r to skill %s", command, md)
            try:
                content, kind = md.read_text(encoding="utf-8", errors="replace"), "skill"
            except OSError:
                logger.error("failed to read skill %s", md, exc_info=True)
        else:
            logger.debug("expansion: no skill folder found for %r", command)

    if content is None:
        logger.debug("expansion: %r not resolved to a command or skill; injecting nothing", command)
        return []

    logger.debug("expansion: injecting %s %r (%d chars)", kind, command, len(content))
    tool_use_id = f"expansion-{command}"
    # Model the expansion as a tool call (``command``/``skill``) plus its result,
    # built through the translator so it matches a real transcript invocation.
    call = translate.tool_use_to_call(
        {"id": tool_use_id, "name": kind, "input": {"command": command, "args": command_args}}
    )
    return [
        Message(role="assistant", tool_calls=[call]),
        Message(role="tool", tool_call_id=tool_use_id, content=content),
    ]


def _find_command_file(cwd: str, command: str) -> Path | None:
    """Locate a Claude Code slash-command markdown file by name.

    Slash commands are markdown files under a ``commands`` directory. Names may
    be plain (``"review-code"`` → ``commands/review-code.md``) or namespaced
    (``"frontend:component"`` → ``commands/frontend/component.md``; the leading
    segment of a namespaced name also scopes the plugin search). The command
    roots are checked in precedence order:

      1. Project-level: ``<cwd>/.claude/commands`` and the same path on every
         ancestor up to the filesystem root (so a command installed at the repo
         root is found from a deeply nested ``cwd``).
      2. User-level: ``<claude_config_dir>/commands``.
      3. Plugin-scoped (for a namespaced name): any
         ``<claude_config_dir>/plugins/**/commands`` whose path contains the
         leading namespace segment.
      4. Fallback: any ``<claude_config_dir>/plugins/**/commands``.

    Within each root we first try the exact relative path (``<rel>.md``). If
    that misses, we fall back to a recursive search for ``<basename>.md`` — a
    nested command (``commands/frontend/component.md``) may be reported by its
    basename alone (``component``) rather than as ``frontend:component``.

    Returns the first existing file, or ``None``.
    """
    if not command:
        return None

    parts = command.split(":")
    # ``frontend:component`` → ``frontend/component.md``; ``foo`` → ``foo.md``.
    leaf = Path(*parts).with_suffix(".md")
    basename = f"{Path(parts[-1]).name}.md" if parts[-1] else ""
    plugin = parts[0] if len(parts) > 1 else ""

    roots: list[Path] = []
    seen: set[Path] = set()

    def _add_root(commands_root: Path) -> None:
        try:
            resolved = commands_root.resolve(strict=False)
        except (OSError, ValueError):
            return
        if resolved in seen:
            return
        seen.add(resolved)
        roots.append(resolved)

    # 1. Project-level: walk up from cwd until the filesystem root.
    if cwd:
        try:
            here = Path(cwd).expanduser().resolve(strict=False)
            for parent in (here, *here.parents):
                _add_root(parent / ".claude" / "commands")
        except (OSError, ValueError):
            logger.debug("find_command: invalid cwd %r", cwd)

    # 2. User-level commands directory.
    claude_home = paths.claude_config_dir()
    _add_root(claude_home / "commands")

    # 3 & 4. Plugin marketplaces — plugin-scoped first, then any.
    plugins_root = claude_home / "plugins"
    if plugins_root.is_dir():
        try:
            commands_dirs = [d for d in plugins_root.rglob("commands") if d.is_dir()]
        except OSError:
            logger.debug("find_command: error walking %s", plugins_root, exc_info=True)
            commands_dirs = []
        if plugin:
            for d in commands_dirs:
                if plugin in d.parts:
                    _add_root(d)
        for d in commands_dirs:
            _add_root(d)

    # Resolve per root in precedence order: within a root, an exact relative
    # path wins, then a recursive ``<basename>.md`` fallback (a nested command
    # like ``commands/frontend/component.md`` may be reported as just
    # ``component``). We must finish both checks for a root before moving to the
    # next, so a higher-precedence project command reported by basename is not
    # beaten by a lower-precedence user/plugin command that matches the exact
    # path — otherwise AI Guard would inspect the wrong definition.
    for root in roots:
        # Exact relative path, guarding against components that escape the
        # commands root (e.g. a ``..`` in the command name).
        target = (root / leaf).resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError:
            target = None
        if target is not None and target.is_file():
            return target

        if basename and root.is_dir():
            try:
                for match in sorted(root.rglob(basename)):
                    if match.is_file():
                        logger.debug(
                            "find_command: resolved %r via basename search to %s", command, match
                        )
                        return match
            except OSError:
                logger.debug("find_command: error searching %s", root, exc_info=True)

    logger.debug("find_command: %r not found (checked %d root(s))", command, len(roots))
    return None


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
