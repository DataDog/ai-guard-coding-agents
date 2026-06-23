# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""End-to-end translation of a real Codex rollout transcript.

``fixtures/rollout_wasm.jsonl`` is a real ``codex-tui`` 0.140.0 session ("Build
me a hello world application in WASM"), trimmed only in the large opaque blobs
(base instructions, encrypted reasoning, the full patch body). It exercises the
full shape Codex emits — including ``custom_tool_call`` / ``custom_tool_call_output``
for ``apply_patch`` and an empty ``function_call_output`` (``rg --files`` with no
matches) — through the handler's transcript reader and the translator.
"""

from __future__ import annotations

from pathlib import Path

from aiguard.codex import handler, translate

FIXTURE = Path(__file__).parent / "fixtures" / "rollout_wasm.jsonl"


def _messages():
    entries = handler._read_transcript(FIXTURE)
    return entries, translate.transcript_to_messages(entries)


def test_fixture_parses_into_expected_role_sequence() -> None:
    entries, messages = _messages()
    # 20 lines in the fixture; only response_items translate, and the two user
    # preamble turns (environment_context + prompt) collapse into one.
    assert len(entries) == 20
    roles = [m["role"] for m in messages]
    assert roles == [
        "system",  # base_instructions + developer permissions/skills block
        "user",  # environment_context + "Build me a hello world application in WASM"
        "assistant",  # "I'll inspect the workspace first"
        "assistant",  # function_call pwd
        "assistant",  # function_call rg --files
        "tool",  # pwd output
        "tool",  # rg --files output (empty)
        "assistant",  # custom_tool_call apply_patch
        "tool",  # apply_patch output
        "assistant",  # final answer
    ]


def test_base_instructions_folded_into_system() -> None:
    _, messages = _messages()
    system = messages[0]
    assert system["role"] == "system"
    # base_instructions part first, then the three developer input_text parts.
    assert len(system["content"]) == 4
    assert system["content"][0]["text"].startswith("You are Codex")
    assert "sandbox_mode" in system["content"][1]["text"]


def test_environment_context_merged_with_prompt() -> None:
    _, messages = _messages()
    users = [m for m in messages if m["role"] == "user"]
    assert len(users) == 1
    parts = [p["text"] for p in users[0]["content"]]
    assert parts[0].startswith("<environment_context>")
    assert parts[1] == "Build me a hello world application in WASM"


def test_exec_commands_become_tool_calls_with_arguments_passthrough() -> None:
    _, messages = _messages()
    calls = [
        tc
        for m in messages
        if m["role"] == "assistant"
        for tc in m.get("tool_calls", [])
    ]
    by_id = {tc["id"]: tc for tc in calls}
    pwd = by_id["call_PPMYtAFOtOwuUevccyKMTKmX"]
    assert pwd["function"]["name"] == "exec_command"
    # arguments are the verbatim JSON string Codex recorded (not re-encoded).
    assert pwd["function"]["arguments"] == (
        '{"cmd":"pwd","workdir":"/home/codex","max_output_tokens":2000}'
    )


def test_empty_command_output_is_empty_string() -> None:
    # ``rg --files`` produced no matches; its tool result content carries the
    # raw chunk wrapper but the model's actual output section is empty.
    _, messages = _messages()
    rg = next(
        m
        for m in messages
        if m["role"] == "tool" and m["tool_call_id"] == "call_Pf6BYw01CkKKHR4f8yMK2MyO"
    )
    assert rg["content"].endswith("Output:\n")


def test_apply_patch_custom_tool_call_is_captured() -> None:
    # Regression: apply_patch is a custom_tool_call, not a function_call. It must
    # surface so AI Guard sees the file modification.
    _, messages = _messages()
    patch_call = next(
        tc
        for m in messages
        if m["role"] == "assistant"
        for tc in m.get("tool_calls", [])
        if tc["id"] == "call_tVR18n9lkwB0eJLWTfHqYwHy"
    )
    assert patch_call["function"]["name"] == "apply_patch"
    # The raw patch body is passed through as the call arguments.
    assert "*** Add File: /home/codex/index.html" in patch_call["function"]["arguments"]

    patch_result = next(
        m
        for m in messages
        if m["role"] == "tool" and m["tool_call_id"] == "call_tVR18n9lkwB0eJLWTfHqYwHy"
    )
    assert "Success. Updated the following files" in patch_result["content"]


def test_reasoning_and_event_msgs_dropped() -> None:
    _, messages = _messages()
    # No encrypted reasoning, token_count, task_started/complete, or session_meta
    # leaks into the evaluated messages.
    assert all(m["role"] in {"system", "user", "assistant", "tool"} for m in messages)
    serialized = str(messages)
    assert "encrypted_content" not in serialized
    assert "task_complete" not in serialized