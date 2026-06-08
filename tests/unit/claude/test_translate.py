# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Unit tests for ``aiguard.claude.translate``.

The translator bridges the Anthropic Messages-API block shape and AI Guard's
OpenAI-shaped message API. Tests are grouped by entry point:

  * :class:`TestEntryToMessages` — per-turn block → message translation.
  * :class:`TestTranscriptToMessages` — whole-transcript translation + order.
  * :class:`TestToolUseToCall` — ``tool_use`` block → tool call.
  * :class:`TestResolveToolContent` — tool-result content normalisation.
"""

from __future__ import annotations

import json

from aiguard.claude.translate import (
    entry_to_messages,
    resolve_tool_content,
    tool_use_to_call,
    transcript_to_messages,
)


class TestEntryToMessages:
    def test_string_content_passes_through(self) -> None:
        entry = {"type": "user", "message": {"role": "user", "content": "hi"}}
        assert entry_to_messages(entry) == [{"role": "user", "content": "hi"}]

    def test_empty_string_content_is_dropped(self) -> None:
        entry = {"type": "user", "message": {"role": "user", "content": ""}}
        assert entry_to_messages(entry) == []

    def test_assistant_text_and_tool_use_both_present(self) -> None:
        entry = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "ok"},
                    {"type": "tool_use", "id": "t", "name": "N", "input": {}},
                ],
            },
        }
        msg = entry_to_messages(entry)[0]
        assert msg["content"][0]["type"] == "text"
        assert msg["tool_calls"][0]["id"] == "t"

    def test_user_tool_result_and_text_split_into_two_messages(self) -> None:
        entry = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu7", "content": "OK"},
                    {"type": "text", "text": "thanks"},
                ],
            },
        }
        roles = [m["role"] for m in entry_to_messages(entry)]
        assert roles == ["tool", "user"]

    def test_non_conversation_entry_returns_empty(self) -> None:
        assert entry_to_messages({"type": "summary"}) == []
        assert entry_to_messages({"type": "user", "message": "not a dict"}) == []

    def test_role_falls_back_to_entry_type_for_list_content(self) -> None:
        # Real transcripts omit ``message.role`` on some turns; the entry
        # ``type`` is the authoritative role and the turn must not be dropped.
        entry = {
            "type": "assistant",
            "message": {
                "id": "msg-001",
                "content": [{"type": "text", "text": "Response 1"}],
                "usage": {"input_tokens": 20, "output_tokens": 10},
            },
        }
        msg = entry_to_messages(entry)[0]
        assert msg["role"] == "assistant"
        assert msg["content"][0]["text"] == "Response 1"

    def test_role_falls_back_to_entry_type_for_string_content(self) -> None:
        entry = {"type": "user", "message": {"content": "hello"}}
        assert entry_to_messages(entry) == [{"role": "user", "content": "hello"}]

    def test_base64_image_becomes_image_url_part(self) -> None:
        entry = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "AAAA",
                        },
                    }
                ],
            },
        }
        part = entry_to_messages(entry)[0]["content"][0]
        assert part["type"] == "image_url"
        assert part["image_url"]["url"] == "data:image/png;base64,AAAA"

    def test_url_image_becomes_image_url_part(self) -> None:
        entry = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}],
            },
        }
        part = entry_to_messages(entry)[0]["content"][0]
        assert part["type"] == "image_url"
        assert part["image_url"]["url"] == "https://x/y.png"

    def test_thinking_block_serialized_as_text_type(self) -> None:
        # AI Guard only accepts ``text``/``image_url`` parts; a ``thinking``
        # block (real shape: ``thinking`` text plus an opaque ``signature``)
        # must be carried as ``text``, not its raw type.
        block = {"type": "thinking", "thinking": "let me reason", "signature": "abc123"}
        entry = {
            "type": "assistant",
            "message": {"role": "assistant", "content": [block]},
        }
        part = entry_to_messages(entry)[0]["content"][0]
        assert part["type"] == "text"
        assert json.loads(part["text"]) == block

    def test_tool_reference_block_serialized_as_text_type(self) -> None:
        # Real Claude Code shape: ``tool_reference`` blocks appear in user turns
        # as ``{"type": "tool_reference", "tool_name": ...}``. AI Guard rejects
        # the raw type, so it must be carried as ``text``.
        entry = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_reference", "tool_name": "WebFetch"}],
            },
        }
        part = entry_to_messages(entry)[0]["content"][0]
        assert part["type"] == "text"
        assert json.loads(part["text"]) == {"type": "tool_reference", "tool_name": "WebFetch"}

    def test_image_without_resolvable_source_falls_back_to_text(self) -> None:
        entry = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "image", "source": {"type": "file", "file_id": "f1"}}],
            },
        }
        part = entry_to_messages(entry)[0]["content"][0]
        assert part["type"] == "text"
        assert json.loads(part["text"])["source"]["file_id"] == "f1"

    def test_tool_use_in_user_turn_still_becomes_tool_call(self) -> None:
        # A ``tool_use`` block is converted to a tool call regardless of the
        # turn's role — never serialized as content.
        entry = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_use", "id": "tu1", "name": "Read", "input": {"p": "x"}}],
            },
        }
        assert entry_to_messages(entry) == [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "tu1", "function": {"name": "Read", "arguments": '{"p": "x"}'}}
                ],
            }
        ]

    def test_tool_result_in_assistant_turn_still_becomes_tool_message(self) -> None:
        entry = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_result", "tool_use_id": "tu9", "content": "done"}],
            },
        }
        assert entry_to_messages(entry) == [
            {"role": "tool", "tool_call_id": "tu9", "content": "done"}
        ]

    def test_tool_result_with_image_content_becomes_image_url_tool_message(self) -> None:
        # Real shape: a screenshot tool result packs an ``image`` block inside
        # the tool_result's ``content`` list (the common image placement).
        entry = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu1",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": "ABC",
                                },
                            }
                        ],
                    }
                ],
            },
        }
        msg = entry_to_messages(entry)[0]
        assert msg["role"] == "tool"
        assert msg["tool_call_id"] == "tu1"
        assert msg["content"] == [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,ABC"}}
        ]

    def test_tool_result_with_tool_reference_content_becomes_text_parts(self) -> None:
        # Real shape: ``tool_reference`` blocks only appear nested inside a
        # tool_result's ``content`` list, often several at once.
        entry = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu1",
                        "content": [
                            {"type": "tool_reference", "tool_name": "WebFetch"},
                            {"type": "tool_reference", "tool_name": "mcp__atlassian__getJiraIssue"},
                        ],
                    }
                ],
            },
        }
        msg = entry_to_messages(entry)[0]
        assert msg["role"] == "tool"
        assert [json.loads(p["text"])["tool_name"] for p in msg["content"]] == [
            "WebFetch",
            "mcp__atlassian__getJiraIssue",
        ]
        assert all(p["type"] == "text" for p in msg["content"])


class TestTranscriptToMessages:
    def test_order_preserved_across_entries(self) -> None:
        entries = [
            {"type": "user", "message": {"role": "user", "content": "run ls"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu1",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "a\nb"}],
                },
            },
            {"type": "summary", "summary": "ignored"},
        ]
        roles = [m["role"] for m in transcript_to_messages(entries)]
        assert roles == ["user", "assistant", "tool"]

    def test_empty_transcript(self) -> None:
        assert transcript_to_messages([]) == []

    def test_metadata_entry_types_are_ignored(self) -> None:
        # Entry types observed in real transcripts that carry no conversation
        # content; every one must be skipped, leaving only the real turns.
        metadata_types = [
            "attachment",
            "permission-mode",
            "ai-title",
            "last-prompt",
            "file-history-snapshot",
            "mode",
            "system",
            "agent-name",
            "pr-link",
            "queue-operation",
            "worktree-state",
            "summary",
        ]
        entries: list[dict] = [{"type": t} for t in metadata_types]
        entries.insert(3, {"type": "user", "message": {"role": "user", "content": "real"}})
        assert transcript_to_messages(entries) == [{"role": "user", "content": "real"}]


class TestToolUseToCall:
    def test_serializes_input_to_arguments(self) -> None:
        call = tool_use_to_call({"id": "x", "name": "Read", "input": {"path": "p"}})
        assert call == {"id": "x", "function": {"name": "Read", "arguments": '{"path": "p"}'}}

    def test_missing_fields_default(self) -> None:
        call = tool_use_to_call({})
        assert call == {"id": "", "function": {"name": "", "arguments": "{}"}}


class TestResolveToolContent:
    def test_string_passes_through(self) -> None:
        assert resolve_tool_content("hello") == "hello"

    def test_none_becomes_empty_string(self) -> None:
        assert resolve_tool_content(None) == ""

    def test_list_becomes_content_parts(self) -> None:
        parts = resolve_tool_content([{"type": "text", "text": "out"}])
        assert parts == [{"type": "text", "text": "out"}]

    def test_empty_list_becomes_empty_list(self) -> None:
        assert resolve_tool_content([]) == []

    def test_dict_is_json_serialized(self) -> None:
        assert resolve_tool_content({"k": "v"}) == '{"k": "v"}'

    def test_non_dict_and_unknown_items_in_list_become_text_parts(self) -> None:
        # Defensive parity with reference parsers: bare/odd items inside a
        # tool_result content list must still map to valid ``text`` parts.
        parts = resolve_tool_content(["bare", 123, {"type": "weird", "k": "v"}])
        assert isinstance(parts, list)
        assert [p["type"] for p in parts] == ["text", "text", "text"]
        assert json.loads(parts[2]["text"]) == {"type": "weird", "k": "v"}

    def test_mixed_blocks_preserve_order_and_types(self) -> None:
        # A tool result may interleave text, images and other blocks; each maps
        # to a valid part type, in order.
        parts = resolve_tool_content(
            [
                {"type": "text", "text": "before"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "Z"},
                },
                {"type": "tool_reference", "tool_name": "WebFetch"},
            ]
        )
        assert isinstance(parts, list)
        assert [p["type"] for p in parts] == ["text", "image_url", "text"]
        assert parts[1]["image_url"]["url"] == "data:image/png;base64,Z"
