# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Unit tests for the Codex rollout → AI Guard message translator."""

from __future__ import annotations

from aiguard.codex import translate
from tests.transcripts import (
    codex_assistant_message,
    codex_custom_tool_call,
    codex_custom_tool_call_output,
    codex_developer_message,
    codex_event_msg,
    codex_function_call,
    codex_function_call_output,
    codex_reasoning,
    codex_session_meta,
    codex_user_message,
)


class TestMessages:
    def test_user_message_input_text(self) -> None:
        msgs = translate.transcript_to_messages([codex_user_message("hello")])
        assert msgs == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]

    def test_assistant_message_output_text(self) -> None:
        msgs = translate.transcript_to_messages([codex_assistant_message("hi there")])
        assert msgs == [{"role": "assistant", "content": [{"type": "text", "text": "hi there"}]}]

    def test_developer_message_maps_to_system(self) -> None:
        msgs = translate.transcript_to_messages([codex_developer_message("be safe")])
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == [{"type": "text", "text": "be safe"}]

    def test_empty_text_parts_dropped(self) -> None:
        entry = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": ""}],
            },
        }
        assert translate.transcript_to_messages([entry]) == []

    def test_string_content_passthrough(self) -> None:
        entry = {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": "raw"},
        }
        assert translate.transcript_to_messages([entry]) == [{"role": "user", "content": "raw"}]


class TestFunctionCalls:
    def test_function_call_arguments_passed_through(self) -> None:
        # Codex stores ``arguments`` as a JSON *string*; it must not be re-encoded.
        args = '{"command":["bash","-lc","ls"]}'
        msgs = translate.transcript_to_messages([codex_function_call("call_1", "shell", args)])
        assert msgs == [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_1", "function": {"name": "shell", "arguments": args}}
                ],
            }
        ]

    def test_function_call_output(self) -> None:
        msgs = translate.transcript_to_messages([codex_function_call_output("call_1", "done")])
        assert msgs == [{"role": "tool", "tool_call_id": "call_1", "content": "done"}]

    def test_custom_tool_call_uses_input_as_arguments(self) -> None:
        # apply_patch is a custom_tool_call: its payload lives in `input` (a raw
        # string), not a JSON `arguments` string — pass it through unchanged.
        patch = "*** Begin Patch\n*** Add File: x.txt\n+hi\n*** End Patch\n"
        msgs = translate.transcript_to_messages(
            [codex_custom_tool_call("call_9", "apply_patch", patch)]
        )
        assert msgs == [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_9", "function": {"name": "apply_patch", "arguments": patch}}
                ],
            }
        ]

    def test_custom_tool_call_output(self) -> None:
        msgs = translate.transcript_to_messages(
            [codex_custom_tool_call_output("call_9", "Success.")]
        )
        assert msgs == [{"role": "tool", "tool_call_id": "call_9", "content": "Success."}]

    def test_function_call_to_call_serialises_object_arguments(self) -> None:
        # The handler builds the pending PreToolUse call from a parsed object.
        call = translate.function_call_to_call(
            {"call_id": "c2", "name": "shell", "arguments": {"command": ["ls"]}}
        )
        assert call["id"] == "c2"
        assert call["function"]["name"] == "shell"
        assert call["function"]["arguments"] == '{"command": ["ls"]}'


class TestDropped:
    def test_reasoning_dropped(self) -> None:
        assert translate.transcript_to_messages([codex_reasoning()]) == []

    def test_event_msg_dropped(self) -> None:
        assert translate.transcript_to_messages([codex_event_msg()]) == []

    def test_session_meta_dropped(self) -> None:
        assert translate.transcript_to_messages([codex_session_meta()]) == []

    def test_unknown_response_item_dropped(self) -> None:
        entry = {"type": "response_item", "payload": {"type": "local_shell_call"}}
        assert translate.transcript_to_messages([entry]) == []

    def test_non_dict_payload_dropped(self) -> None:
        assert translate.transcript_to_messages([{"type": "response_item", "payload": None}]) == []


class TestFullConversation:
    def test_ordered_round_trip(self) -> None:
        entries = [
            codex_session_meta(),
            codex_developer_message("env"),
            codex_user_message("find the bug"),
            codex_reasoning(),
            codex_assistant_message("on it"),
            codex_function_call("c1", "shell", '{"command":["rg","bug"]}'),
            codex_function_call_output("c1", "src/x.py:1: bug"),
            codex_event_msg("token_count", "n/a"),
        ]
        msgs = translate.transcript_to_messages(entries)
        roles = [m["role"] for m in msgs]
        assert roles == ["system", "user", "assistant", "assistant", "tool"]
        assert msgs[3]["tool_calls"][0]["id"] == "c1"
        assert msgs[4]["content"] == "src/x.py:1: bug"
