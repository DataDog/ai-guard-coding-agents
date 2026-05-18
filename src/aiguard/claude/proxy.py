"""Claude/Anthropic proxy — registers the ``proxy claude`` subcommand."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import aiohttp.web
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

from aiguard.claude.util import fetch_user_id
from aiguard.constants import AIGuardConstants
from aiguard.proxy.server import ProxyHandler
from aiguard.proxy.util import parse_sse
from aiguard.storage import delete_messages, load_messages

logger = logging.getLogger("ai_guard")

# CamelCase → snake_case for the dispatched method suffix
# (``SessionStart`` → ``session_start``).
_CAMEL_TO_SNAKE = re.compile(r"(?<!^)(?=[A-Z])")


class ClaudeProxy(ProxyHandler):
    """Handler for the Anthropic Messages API (``/v1/messages``)."""

    def __init__(self, upstream: str, blocking: bool) -> None:
        self._upstream = upstream
        self._blocking = blocking
        self._ai_guard = new_ai_guard_client(meta={"coding_agent": AIGuardConstants.CLAUDE_CODE})

    def agent(self) -> str:
        return "claude"

    def upstream(self) -> str:
        return self._upstream

    def matches(self, request: aiohttp.web.Request) -> bool:
        user_agent = request.headers.get("User-Agent", "")
        return "claude-cli" in user_agent

    def parse_request(self, request: aiohttp.web.Request, body: bytes) -> tuple[str, list[Message]]:
        if (
            request.method.upper() != "POST"
            or request.path.split("?")[0] != "/v1/messages"
            or "application/json" not in request.content_type.lower()
        ):
            return "", []
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            logger.error("failed to parse request body", exc_info=True)
            return "", []
        return _fetch_session_id(data), _parse_request_body(data)

    def parse_response(self, response: aiohttp.ClientResponse, body: bytes) -> list[Message]:
        content_type = response.content_type.lower()
        return (
            _parse_sse_body(body)
            if "text/event-stream" in content_type
            else _parse_body(content_type, body)
        )

    async def handle_hook(self, hook: str, payload: bytes) -> bytes:
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

        result = await method(event)
        return b"" if result is None else json.dumps(result, ensure_ascii=False).encode()

    # ── Hook handlers ─────────────────────────────────────────────────────────
    # Method name is ``_<hook_in_snake_case>``. ``@tracer.wrap`` opens the span;
    # tags are set on the active span via ``tracer.current_span()``.

    @tracer.wrap(name=AIGuardConstants.SESSION_START, resource=AIGuardConstants.HOOK_RESOURCE)
    async def _session_start(self, event: dict[str, Any]) -> dict[str, Any] | None:
        _set_common_tags(event)
        return None

    @tracer.wrap(name=AIGuardConstants.SESSION_END, resource=AIGuardConstants.HOOK_RESOURCE)
    async def _session_end(self, event: dict[str, Any]) -> dict[str, Any] | None:
        tags = _set_common_tags(event)
        # The conversation history is no longer needed — clear the per-session file.
        session_id = tags[AIGuardConstants.SESSION_ID_TAG]
        if session_id:
            delete_messages(self.agent(), session_id)
        return None

    @tracer.wrap(name=AIGuardConstants.SUBAGENT_START, resource=AIGuardConstants.HOOK_RESOURCE)
    async def _subagent_start(self, event: dict[str, Any]) -> dict[str, Any] | None:
        _set_common_tags(event)
        return None

    @tracer.wrap(name=AIGuardConstants.SUBAGENT_STOP, resource=AIGuardConstants.HOOK_RESOURCE)
    async def _subagent_stop(self, event: dict[str, Any]) -> dict[str, Any] | None:
        _set_common_tags(event)
        return None

    @tracer.wrap(name=AIGuardConstants.PRE_TOOL, resource=AIGuardConstants.HOOK_RESOURCE)
    async def _pre_tool_use(self, event: dict[str, Any]) -> dict[str, Any] | None:
        tags = _set_common_tags(event)
        session_id = tags[AIGuardConstants.SESSION_ID_TAG]
        messages = load_messages(self.agent(), session_id)
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
            await self._evaluate_messages(messages, tags)
        except AIGuardAbortError as e:
            logger.error("PreToolUse: blocked tool '%s', reason=%s", tool_name, e.reason)
            return _blocked_tool_response(event, e)

        return None

    @tracer.wrap(name=AIGuardConstants.POST_TOOL, resource=AIGuardConstants.HOOK_RESOURCE)
    async def _post_tool_use(self, event: dict[str, Any]) -> dict[str, Any] | None:
        tags = _set_common_tags(event)
        session_id = tags[AIGuardConstants.SESSION_ID_TAG]
        messages = load_messages(self.agent(), session_id)
        tool_response = event.get("tool_response", "")
        messages.append(
            Message(
                role="tool",
                tool_call_id=event.get("tool_use_id", ""),
                content=_resolve_tool_content(tool_response) or "",
            )
        )
        try:
            await self._evaluate_messages(messages, tags)
        except AIGuardAbortError as e:
            return _blocked_tool_response(event, e)

        return None

    @tracer.wrap(name=AIGuardConstants.POST_TOOL, resource=AIGuardConstants.HOOK_RESOURCE)
    async def _post_tool_use_failure(self, event: dict[str, Any]) -> dict[str, Any] | None:
        tags = _set_common_tags(event)
        session_id = tags[AIGuardConstants.SESSION_ID_TAG]
        messages = load_messages(self.agent(), session_id)
        messages.append(
            Message(
                role="tool",
                tool_call_id=event.get("tool_use_id", ""),
                content=event.get("error", ""),
            )
        )

        try:
            await self._evaluate_messages(messages, tags)
        except AIGuardAbortError as e:
            tool_name = event.get("tool_name", "")
            logger.error("PostToolUseFailure: blocked tool '%s', reason=%s", tool_name, e.reason)
            return _blocked_tool_response(event, e)

        return None

    async def _evaluate_messages(self, messages: list[Message], tags: dict[str, Any]) -> None:
        if messages:
            try:
                self._ai_guard.evaluate(messages, Options(block=self._blocking, tags=tags))
            except AIGuardAbortError:
                raise
            except Exception:
                logger.error("message evaluation with AI Guard failed", exc_info=True)
        return None


# ── Helpers ──────────────────────────────────────────────────────────────


def _set_common_tags(event: dict[str, Any]) -> dict[str, Any]:
    tags: dict[str, Any] = {
        AIGuardConstants.CODING_AGENT_TAG: AIGuardConstants.CLAUDE_CODE,
    }

    if "model" in event:
        tags[AIGuardConstants.MODEL_TAG] = event["model"]

    email = fetch_user_id()
    if email:
        tags[AIGuardConstants.USER_ID_TAG] = email

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

    instructions = [
        "",
        "In your next reply, write a short user-facing message that:",
        "1. States that Datadog AI Guard blocked the call to the tool above.",
        "2. Names the most likely risk category and includes its confidence as a percentage, "
        "also include other categories if they have high probabilities."
        "3. Suggests sensible next steps (rephrase the request, review the input, inspect the "
        "affected file, or contact the user's security team).",
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


def _append_content_part(
    content: str | list[ContentPart] | None, part: ContentPart
) -> list[ContentPart]:
    """Return ``content`` extended with ``part``, normalizing to a ``ContentPart`` list.

    Tool-result and message content can be either a plain string or a
    ``list[ContentPart]``. Both are collapsed into a list so callers can append
    extra parts without caring about the original shape: a non-empty string is
    wrapped in ``ContentPart(type="text", …)`` and ``part`` is appended.
    """
    if isinstance(content, list):
        return [*content, part]
    if isinstance(content, str) and content:
        return [ContentPart(type="text", text=content), part]
    return [part]


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


def _build_assistant_messages(content: list) -> list[Message]:
    # TODO(@manuel-alvarez-alvarez): evaluate thinking blocks with AI Guard
    # once the API supports them.
    text_parts = _to_content_parts(content, exclude_types={"thinking", "tool_use"})
    tool_calls = [
        ToolCall(
            id=b.get("id", ""),
            function=Function(
                name=b.get("name", ""),
                arguments=json.dumps(b.get("input", {})),
            ),
        )
        for b in content
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    if not text_parts and not tool_calls:
        return []
    msg: Message = {"role": "assistant"}
    if text_parts:
        msg["content"] = text_parts
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return [msg]


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


def _parse_anthropic_message(msg: dict) -> list[Message]:
    """Convert a single Anthropic message dict to a list of AI Guard Messages.

    Preserves all content block types by serializing non-text blocks into ContentPart.text JSON.
    """
    role = msg.get("role", "")
    content = msg.get("content", "")
    if not role:
        return []

    results: list[Message] = []
    if isinstance(content, str):
        results.append(Message(role=role, content=content))
        return results

    if not isinstance(content, list):
        return results

    if role == "assistant":
        results.extend(_build_assistant_messages(content))
        return results

    if role == "user":
        # 1) Emit tool results as separate tool-role messages
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                tool_content = _resolve_tool_content(block.get("content", ""))
                tm: Message = {"role": "tool", "tool_call_id": block.get("tool_use_id", "")}
                if tool_content:
                    tm["content"] = tool_content
                results.append(tm)

        # 2) Emit a user message preserving all non-tool_result content blocks
        parts = _to_content_parts(content, exclude_types={"tool_result"})
        if parts:
            results.append(Message(role="user", content=parts))

    return results


def _parse_request_body(data: dict) -> list[Message]:
    result: list[Message] = []

    system = data.get("system", "")
    if isinstance(system, str) and system:
        result.append(Message(role="system", content=system))
    elif isinstance(system, list):
        parts = _to_content_parts(system)
        if parts:
            result.append(Message(role="system", content=parts))

    for msg in data.get("messages", []):
        result.extend(_parse_anthropic_message(msg))

    return result


def _fetch_session_id(data: dict[str, Any]) -> str:
    """Extract the Claude session id from an Anthropic Messages request body.

    Claude Code embeds session metadata as a JSON-encoded string in
    ``metadata.user_id``. Returns ``""`` when missing or malformed.
    """
    raw_user_id = (data.get("metadata") or {}).get("user_id", "")
    if not isinstance(raw_user_id, str) or not raw_user_id:
        return ""
    try:
        parsed = json.loads(raw_user_id)
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    sid = parsed.get("session_id", "")
    return sid if isinstance(sid, str) else ""


def _parse_body(content_type: str, body: bytes) -> list[Message]:
    if "application/json" not in content_type:
        return []
    data = json.loads(body)
    return _parse_anthropic_message(data)


def _parse_sse_body(body: bytes) -> list[Message]:
    """Assemble an assistant Message from a buffered Anthropic SSE stream.

    Per content-block index we accumulate text/tool/thinking deltas and serialize
    non-text blocks (image, etc.) as JSON-text ContentParts.
    """
    blocks: dict[int, dict[str, Any]] = {}

    def _payload_to_json_text(raw: object) -> str:
        try:
            if hasattr(raw, "model_dump"):
                raw = raw.model_dump()
            return json.dumps(raw, ensure_ascii=False)
        except Exception:
            return str(raw)

    for chunk in parse_sse(body):
        if chunk.data == "[DONE]":
            continue
        try:
            event = json.loads(chunk.data)
        except (json.JSONDecodeError, ValueError):
            continue

        etype = event.get("type")
        if etype == "content_block_start":
            idx = int(event.get("index", 0))
            block = event.get("content_block", {}) or {}
            btype = block.get("type", "unknown")
            state: dict[str, Any] = {"type": btype, "raw": block}
            if btype == "text":
                state["text"] = ""
            elif btype == "thinking":
                # TODO(@manuel-alvarez-alvarez): evaluate thinking blocks
                # once AI Guard supports them.
                state["thinking"] = ""
            elif btype == "tool_use":
                state.update(
                    {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "input_json": "",
                    }
                )
            blocks[idx] = state

        elif etype == "content_block_delta":
            idx = int(event.get("index", 0))
            delta = event.get("delta", {}) or {}
            dtype = delta.get("type")
            state = blocks.get(idx)
            if not state:
                continue
            if dtype == "text_delta":
                state["text"] = state.get("text", "") + (delta.get("text", "") or "")
            elif dtype == "input_json_delta":
                state["input_json"] = state.get("input_json", "") + (
                    delta.get("partial_json", "") or ""
                )
            elif dtype == "thinking_delta":
                # thinking event uses field 'thinking'
                state["thinking"] = state.get("thinking", "") + (delta.get("thinking", "") or "")

        # message_start / content_block_stop / message_stop are not needed for
        # buffered reconstruction; only start/delta carry payload.

    indices = sorted(blocks.keys())
    content_parts: list[ContentPart] = []
    tool_calls: list[ToolCall] = []
    for idx in indices:
        st = blocks[idx]
        btype = st.get("type", "unknown")
        if btype == "text":
            text = st.get("text", "")
            if text:
                content_parts.append(ContentPart(type="text", text=text))
        elif btype == "thinking":
            # TODO(@manuel-alvarez-alvarez): evaluate thinking blocks once
            # AI Guard supports them.
            pass
        elif btype == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=st.get("id", ""),
                    function=Function(name=st.get("name", ""), arguments=st.get("input_json", "")),
                )
            )
        else:
            # Preserve any other content block (e.g., image)
            payload = _payload_to_json_text(st.get("raw", {}))
            content_parts.append(ContentPart(type=btype, text=payload))

    if not content_parts and not tool_calls:
        return []
    msg: Message = {"role": "assistant"}
    if content_parts:
        msg["content"] = content_parts
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return [msg]


def _find_skill_folder(cwd: str, skill: str) -> Path | None:
    """Locate a Claude Code skill folder by name.

    A skill folder is identified by a ``SKILL.md`` marker file. Names may be
    plain (``"foo"``) or namespaced (``"plugin:foo"`` — the prefix scopes the
    search to that plugin first). All known install locations are checked, in
    precedence order:

      1. Project-level: ``<cwd>/.claude/skills/<name>`` and the same path on
         every ancestor up to the filesystem root (so a skill installed at
         the repo root is found from a deeply nested ``cwd``).
      2. User-level: ``~/.claude/skills/<name>``.
      3. Plugin-scoped (when the ``"plugin:"`` prefix is present): any
         ``~/.claude/plugins/**/skills/<name>`` whose path contains the plugin
         segment.
      4. Fallback: any ``~/.claude/plugins/**/skills/<name>``.

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
    home = Path.home()
    _add(home / ".claude" / "skills", name)

    # 3 & 4. Plugin marketplaces — plugin-scoped first, then any.
    plugins_root = home / ".claude" / "plugins"
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
