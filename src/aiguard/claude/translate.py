# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Translate Anthropic Messages-API content into AI Guard messages.

Translation table — Anthropic transcript → AI Guard message(s):

  entry.type           Anthropic shape                AI Guard output
  ───────────────────  ─────────────────────────────  ──────────────────────────
  user / assistant     message.content = "str"        message {role, content:str}
  user / assistant     message.content = [blocks]     per-block (rows below)
  anything else        mode, summary, snapshot, …      dropped

  block                Anthropic shape                AI Guard output
  ───────────────────  ─────────────────────────────  ──────────────────────────
  text                 {type, text}                   part {type:text, text}
  image                {type, source:{base64}}        part {type:image_url} data:
  image                {type, source:{url}}           part {type:image_url} url
  image                unresolved source              part {type:text} block JSON
  tool_use             {type, id, name, input}        assistant {tool_calls:[…]}
  tool_result          {type, tool_use_id, content}   tool {tool_call_id, content}
  thinking (no text)   {type, signature}              dropped (AI Guard can't handle)
  anything else        thinking text, tool_reference  part {type:text} block JSON

  tool_result.content  "str" / None                   passed through ("" if None)
  tool_result.content  [blocks]                       parts (blocks, as above)
  tool_result.content  other JSON                     JSON string

"""

from __future__ import annotations

import json
from typing import Any

from aiguard.client import ContentPart, Function, ImageURL, Message, ToolCall

__all__ = [
    "transcript_to_messages",
    "entry_to_messages",
    "tool_use_to_call",
    "resolve_tool_content",
]


def transcript_to_messages(entries: list[dict[str, Any]]) -> list[Message]:
    """Translate a sequence of transcript entries into AI Guard messages."""
    messages: list[Message] = []
    for entry in entries:
        messages.extend(entry_to_messages(entry))
    return messages


def entry_to_messages(entry: dict[str, Any]) -> list[Message]:
    """Translate one transcript entry into zero or more AI Guard messages."""
    entry_type = entry.get("type")
    if entry_type not in ("user", "assistant"):
        return []
    message = entry.get("message")
    if not isinstance(message, dict):
        return []

    # ``message.role`` is sometimes omitted; the entry ``type`` is the
    # authoritative role for user/assistant turns, so fall back to it.
    role = message.get("role") or entry_type
    content = message.get("content")

    if isinstance(content, str):
        return [Message(role=role, content=content)] if content else []
    if not isinstance(content, list):
        return []

    return _blocks_to_messages(content, role)


def _blocks_to_messages(blocks: list, role: str) -> list[Message]:
    """Translate one turn's content blocks into AI Guard messages (see table)."""
    tool_messages: list[Message] = []
    tool_calls: list[ToolCall] = []
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if btype == "tool_use":
            tool_calls.append(tool_use_to_call(b))
        elif btype == "tool_result":
            tool_messages.append(
                Message(
                    role="tool",
                    tool_call_id=b.get("tool_use_id", ""),
                    content=resolve_tool_content(b.get("content")),
                )
            )

    parts = _blocks_to_content_parts(blocks, exclude_types={"tool_use", "tool_result"})

    messages: list[Message] = list(tool_messages)
    if tool_calls:
        assistant: Message = {"role": "assistant"}
        # Fold this turn's text into the assistant message that issued the tool
        # calls when the turn is the assistant's own.
        if role == "assistant" and parts:
            assistant["content"] = parts
            parts = []
        assistant["tool_calls"] = tool_calls
        messages.append(assistant)

    if parts:
        messages.append(Message(role=role, content=parts))

    return messages


def tool_use_to_call(block: dict) -> ToolCall:
    """Convert an Anthropic ``tool_use`` block into an AI Guard tool call."""
    try:
        arguments = json.dumps(block.get("input", {}), ensure_ascii=False)
    except (TypeError, ValueError):
        arguments = "{}"
    return ToolCall(
        id=block.get("id", ""),
        function=Function(name=block.get("name", ""), arguments=arguments),
    )


def resolve_tool_content(raw: Any) -> str | list[ContentPart]:
    """Normalise a tool-result's content into AI Guard message content (see table)."""
    if isinstance(raw, list):
        parts = _blocks_to_content_parts(raw)
        return parts or []
    # For non-list content, pass through string content as-is; otherwise JSON-serialize
    if isinstance(raw, str) or raw is None:
        return raw or ""
    try:
        return json.dumps(raw, ensure_ascii=False)
    except Exception:
        return str(raw)


def _blocks_to_content_parts(
    blocks: list, *, exclude_types: set[str] | None = None
) -> list[ContentPart]:
    """Convert a list of blocks to ContentParts, optionally excluding some types."""
    parts: list[ContentPart] = []
    for b in blocks or []:
        if isinstance(b, dict) and exclude_types and (b.get("type") in exclude_types):
            continue
        cp = _block_to_content_part(b)
        if cp is not None:
            parts.append(cp)
    return parts


def _block_to_content_part(block: object) -> ContentPart | None:
    """Convert one Anthropic content block to a ``text``/``image_url`` part (see table)."""
    if not isinstance(block, dict):
        try:
            return ContentPart(type="text", text=json.dumps(block, ensure_ascii=False))
        except Exception:
            return None

    btype = block.get("type")
    if btype == "text":
        text = block.get("text", "")
        return ContentPart(type="text", text=text) if text else None

    # TODO: AI Guard cannot handle thinking content. Drop signature-only
    # ``thinking`` blocks (empty ``thinking`` text, just an opaque signature);
    # blocks carrying actual reasoning text still fall through to JSON text.
    if btype == "thinking" and not block.get("thinking"):
        return None

    if btype == "image":
        url = _image_block_to_url(block)
        if url:
            return ContentPart(type="image_url", image_url=ImageURL(url=url))

    try:
        payload = json.dumps(block, ensure_ascii=False)
    except Exception:
        payload = str(block)
    return ContentPart(type="text", text=payload)


def _image_block_to_url(block: dict) -> str | None:
    """Return an image URL (hosted, or ``data:`` for base64) or ``None`` if unresolved."""
    source = block.get("source")
    if not isinstance(source, dict):
        return None
    if source.get("type") == "url":
        url = source.get("url")
        return url if isinstance(url, str) and url else None
    if source.get("type") == "base64":
        data = source.get("data")
        if isinstance(data, str) and data:
            media_type = source.get("media_type") or "application/octet-stream"
            return f"data:{media_type};base64,{data}"
    return None
