# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Translate Codex CLI rollout entries into AI Guard messages.

Codex stores each session as a rollout JSONL file
(``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl``). Every line is
``{"type", "timestamp", "payload"}``. The conversation lives in the
``response_item`` lines, which use the OpenAI **Responses API** item shape
(not Anthropic content blocks). Other line types mirror the same events for the
TUI and carry no extra signal, so they are dropped.

Translation table — Codex rollout entry → AI Guard message(s):

  entry.type        payload.type               AI Guard output
  ────────────────  ─────────────────────────  ──────────────────────────────────
  response_item     message                    per-role message (rows below)
  response_item     function_call              assistant {tool_calls:[…]}
  response_item     function_call_output       tool {tool_call_id, content}
  response_item     custom_tool_call           assistant {tool_calls:[…]}  (apply_patch)
  response_item     custom_tool_call_output    tool {tool_call_id, content}
  response_item     reasoning                  dropped (AI Guard can't handle it)
  response_item     other                      dropped
  session_meta      base_instructions.text     prepended into the system message
  turn_context      —                          dropped (metadata)
  event_msg         —                          dropped (TUI mirror of the above)

Two preamble-specific merges reassemble what Codex splits across items: the
session-level ``base_instructions`` (the base system prompt) is folded into the
``developer``-derived ``system`` message, and the injected ``<environment_context>``
``user`` turn is merged with the user's first real prompt into one ``user``
message with multiple content parts. Nothing else is coalesced.

  message.role      AI Guard role
  ────────────────  ─────────────────────────────────────────────────────────
  user              user
  assistant         assistant
  developer         system   (Codex injects env/permission instructions here)
  other             passed through verbatim

  message.content   Responses shape                 AI Guard output
  ────────────────  ──────────────────────────────  ──────────────────────────
  input_text        {type, text}                    part {type:text, text}
  output_text       {type, text}                    part {type:text, text}
  other / non-dict  anything                         part {type:text} block JSON

  function_call.arguments   already a JSON string    passed through verbatim
  custom_tool_call.input    raw string (e.g. patch)  passed through as arguments
  function_call_output.output  str / other           str passed through; else JSON
"""

from __future__ import annotations

import json
from typing import Any

from aiguard.client import ContentPart, Function, Message, ToolCall

__all__ = [
    "transcript_to_messages",
    "entry_to_messages",
    "function_call_to_call",
    "resolve_tool_content",
]

# Responses ``message`` roles that don't map 1:1 to an AI Guard role.
_ROLE_MAP = {"developer": "system"}


def transcript_to_messages(entries: list[dict[str, Any]]) -> list[Message]:
    """Translate a sequence of rollout entries into AI Guard messages.

    See the module docstring for the two preamble-specific merges applied here
    (base instructions → system; ``<environment_context>`` → first user prompt).
    """
    messages: list[Message] = []
    for entry in entries:
        messages.extend(entry_to_messages(entry))

    _fold_base_instructions(messages, _base_instructions(entries))
    _merge_environment_context(messages)
    return messages


def _base_instructions(entries: list[dict[str, Any]]) -> str | None:
    """Return the ``base_instructions`` text from the first ``session_meta``."""
    for entry in entries:
        if entry.get("type") == "session_meta":
            payload = entry.get("payload")
            block = payload.get("base_instructions") if isinstance(payload, dict) else None
            text = block.get("text") if isinstance(block, dict) else None
            return text if isinstance(text, str) and text else None
    return None


def _fold_base_instructions(messages: list[Message], base_instructions: str | None) -> None:
    """Prepend the base system prompt into the developer-derived system message."""
    if not base_instructions:
        return
    part = ContentPart(type="text", text=base_instructions)
    for message in messages:
        if message.get("role") == "system":
            message["content"] = [part] + _as_parts(message.get("content"))
            return
    messages.insert(0, Message(role="system", content=[part]))


def _merge_environment_context(messages: list[Message]) -> None:
    """Merge the injected ``<environment_context>`` user turn into the next prompt."""
    for i in range(len(messages) - 1):
        cur, nxt = messages[i], messages[i + 1]
        if cur.get("role") == "user" and nxt.get("role") == "user" and _is_environment_context(cur):
            cur["content"] = _as_parts(cur.get("content")) + _as_parts(nxt.get("content"))
            del messages[i + 1]
            return


def _is_environment_context(message: Message) -> bool:
    content = message.get("content")
    if isinstance(content, str):
        return content.lstrip().startswith("<environment_context>")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        return content[0].get("text", "").lstrip().startswith("<environment_context>")
    return False


def _as_parts(content: Any) -> list[ContentPart]:
    """Normalise a message's content to a list of content parts for merging."""
    if isinstance(content, list):
        return list(content)
    if isinstance(content, str) and content:
        return [ContentPart(type="text", text=content)]
    return []


def entry_to_messages(entry: dict[str, Any]) -> list[Message]:
    """Translate one rollout entry into zero or more AI Guard messages."""
    if entry.get("type") != "response_item":
        return []
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return []

    item_type = payload.get("type")
    if item_type == "message":
        return _message_to_messages(payload)
    # ``function_call`` is the shell/MCP tool; ``custom_tool_call`` is how Codex
    # records freeform tools like ``apply_patch`` (the file-edit tool) — both are
    # assistant tool calls we must surface so AI Guard sees the action.
    if item_type in ("function_call", "custom_tool_call"):
        return [Message(role="assistant", tool_calls=[function_call_to_call(payload)])]
    if item_type in ("function_call_output", "custom_tool_call_output"):
        return [
            Message(
                role="tool",
                tool_call_id=payload.get("call_id", ""),
                content=resolve_tool_content(payload.get("output")),
            )
        ]
    # ``reasoning`` (and anything else) carries no content AI Guard can use.
    return []


def _message_to_messages(payload: dict[str, Any]) -> list[Message]:
    """Translate a Responses ``message`` item into a single AI Guard message."""
    role = _ROLE_MAP.get(payload.get("role", ""), payload.get("role", "user"))
    content = payload.get("content")

    if isinstance(content, str):
        return [Message(role=role, content=content)] if content else []
    if not isinstance(content, list):
        return []

    parts = _content_to_parts(content)
    return [Message(role=role, content=parts)] if parts else []


def function_call_to_call(payload: dict[str, Any]) -> ToolCall:
    """Convert a Responses tool-call item into an AI Guard tool call.

    Unlike Anthropic ``tool_use`` blocks (whose ``input`` is an object we
    serialise), Codex already stores ``function_call`` ``arguments`` as a JSON
    string, so it is passed through verbatim. ``custom_tool_call`` items (e.g.
    ``apply_patch``) carry their payload in ``input`` instead — a raw string we
    pass through unchanged so AI Guard sees the patch body.
    """
    arguments = payload.get("arguments")
    if arguments is None:
        arguments = payload.get("input", "")
    if not isinstance(arguments, str):
        try:
            arguments = json.dumps(arguments, ensure_ascii=False)
        except (TypeError, ValueError):
            arguments = "{}"
    return ToolCall(
        id=payload.get("call_id", ""),
        function=Function(name=payload.get("name", ""), arguments=arguments),
    )


def resolve_tool_content(raw: Any) -> str | list[ContentPart]:
    """Normalise a ``function_call_output`` payload into AI Guard content.

    Codex writes the output as a string (often itself JSON); pass strings
    through as-is and JSON-serialise anything else.
    """
    if isinstance(raw, str) or raw is None:
        return raw or ""
    if isinstance(raw, list):
        return _content_to_parts(raw)
    try:
        return json.dumps(raw, ensure_ascii=False)
    except Exception:
        return str(raw)


def _content_to_parts(content: list) -> list[ContentPart]:
    """Convert a Responses ``content`` array into AI Guard content parts."""
    parts: list[ContentPart] = []
    for item in content or []:
        part = _content_item_to_part(item)
        if part is not None:
            parts.append(part)
    return parts


def _content_item_to_part(item: object) -> ContentPart | None:
    """Convert one Responses content item to a ``text`` part (see table)."""
    if not isinstance(item, dict):
        try:
            return ContentPart(type="text", text=json.dumps(item, ensure_ascii=False))
        except Exception:
            return None

    if item.get("type") in ("input_text", "output_text", "text"):
        text = item.get("text", "")
        return ContentPart(type="text", text=text) if text else None

    try:
        payload = json.dumps(item, ensure_ascii=False)
    except Exception:
        payload = str(item)
    return ContentPart(type="text", text=payload)
