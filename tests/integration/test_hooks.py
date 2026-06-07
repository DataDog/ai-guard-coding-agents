# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Integration tests for the ``ai-guard hook`` command.

These drive the real ``hook`` Click command end-to-end against the real
``ClaudeHandler`` — only the AI Guard client is faked (via the autouse
``fake_ai_guard`` fixture). The handler reconstructs conversation history from
on-disk Claude Code transcripts, so each test lays down a transcript with the
``transcripts`` fixture and points the hook payload's ``transcript_path`` at it.

Covered:
  * PreToolUse evaluates the transcript and allows / blocks.
  * PostToolUse appends the tool result (and de-dupes if the transcript already
    has it).
  * Subagent calls evaluate the subagent's own transcript.
  * Lifecycle hooks and unknown agents are graceful no-ops.
  * A failed/garbage invocation never breaks the caller (exit 0).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner
from ddtrace.appsec.ai_guard import AIGuardAbortError

from aiguard.hooks.hooks import hook
from tests.transcripts import (
    TranscriptWriter,
    assistant_text,
    assistant_tool_use,
    tool_result,
    user_text,
)

SESSION = "sess-int-1"


def _invoke(hook_name: str, event: dict[str, Any], *, block: bool = True) -> Any:
    # Blocking is driven by DD_AI_GUARD_BLOCK (sourced from config.env at runtime).
    env = {} if block else {"DD_AI_GUARD_BLOCK": "false"}
    return CliRunner().invoke(
        hook, ["claude", hook_name], input=json.dumps(event).encode(), env=env
    )


def _pre_tool_event(transcript_path: str, **extra: Any) -> dict[str, Any]:
    event = {
        "hook_event_name": "PreToolUse",
        "session_id": SESSION,
        "transcript_path": transcript_path,
        "tool_name": "Bash",
        "tool_use_id": "tu1",
    }
    event.update(extra)
    return event


@pytest.mark.usefixtures("tmp_home", "fake_endpoint_id")
class TestPreToolUse:
    def test_evaluates_transcript_history_and_allows(
        self, transcripts: TranscriptWriter, fake_ai_guard
    ) -> None:
        path = transcripts.write_main(
            SESSION,
            [user_text("list the files"), assistant_tool_use("tu1", "Bash", {"command": "ls"})],
        )

        result = _invoke("PreToolUse", _pre_tool_event(path))

        assert result.exit_code == 0, result.output
        assert result.output == ""  # allowed → no additionalContext written
        assert len(fake_ai_guard.calls) == 1
        messages = fake_ai_guard.last_messages
        assert messages[0]["role"] == "user"
        assert messages[-1]["role"] == "assistant"
        assert messages[-1]["tool_calls"][0]["function"]["name"] == "Bash"

    def test_block_emits_deny_decision(self, transcripts: TranscriptWriter, fake_ai_guard) -> None:
        path = transcripts.write_main(SESSION, [user_text("rm everything")])
        fake_ai_guard.queue_abort(
            AIGuardAbortError(
                action="DENY",
                reason="destructive_action",
                tags=["destructive"],
                tag_probs={"destructive": 0.97},
            )
        )

        result = _invoke("PreToolUse", _pre_tool_event(path))

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        decision = payload["hookSpecificOutput"]
        assert decision["hookEventName"] == "PreToolUse"
        assert decision["permissionDecision"] == "deny"
        assert "destructive" in decision["additionalContext"]

    def test_observe_only_mode_still_evaluates(
        self, transcripts: TranscriptWriter, fake_ai_guard
    ) -> None:
        path = transcripts.write_main(SESSION, [user_text("hi")])
        result = _invoke("PreToolUse", _pre_tool_event(path), block=False)
        assert result.exit_code == 0
        assert fake_ai_guard.calls[0][1].get("block") is False


@pytest.mark.usefixtures("tmp_home", "fake_endpoint_id")
class TestPostToolUse:
    def test_appends_tool_result_to_history(
        self, transcripts: TranscriptWriter, fake_ai_guard
    ) -> None:
        path = transcripts.write_main(
            SESSION,
            [user_text("run ls"), assistant_tool_use("tu1", "Bash", {"command": "ls"})],
        )

        event = {
            "hook_event_name": "PostToolUse",
            "session_id": SESSION,
            "transcript_path": path,
            "tool_name": "Bash",
            "tool_use_id": "tu1",
            "tool_response": "file_a\nfile_b",
        }
        result = _invoke("PostToolUse", event)

        assert result.exit_code == 0, result.output
        last = fake_ai_guard.last_messages[-1]
        assert last["role"] == "tool"
        assert last["tool_call_id"] == "tu1"
        assert last["content"] == "file_a\nfile_b"

    def test_does_not_duplicate_result_already_in_transcript(
        self, transcripts: TranscriptWriter, fake_ai_guard
    ) -> None:
        path = transcripts.write_main(
            SESSION,
            [
                user_text("run ls"),
                assistant_tool_use("tu1", "Bash", {"command": "ls"}),
                tool_result("tu1", "already recorded"),
            ],
        )

        event = {
            "hook_event_name": "PostToolUse",
            "session_id": SESSION,
            "transcript_path": path,
            "tool_name": "Bash",
            "tool_use_id": "tu1",
            "tool_response": "from-hook-payload",
        }
        result = _invoke("PostToolUse", event)

        assert result.exit_code == 0
        tool_msgs = [m for m in fake_ai_guard.last_messages if m.get("tool_call_id") == "tu1"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["content"] == "already recorded"

    def test_failure_hook_appends_error(self, transcripts: TranscriptWriter, fake_ai_guard) -> None:
        path = transcripts.write_main(
            SESSION, [assistant_tool_use("tu9", "Bash", {"command": "boom"})]
        )
        event = {
            "hook_event_name": "PostToolUseFailure",
            "session_id": SESSION,
            "transcript_path": path,
            "tool_name": "Bash",
            "tool_use_id": "tu9",
            "error": "command not found",
        }
        result = _invoke("PostToolUseFailure", event)

        assert result.exit_code == 0
        last = fake_ai_guard.last_messages[-1]
        assert last["role"] == "tool"
        assert last["content"] == "command not found"


@pytest.mark.usefixtures("tmp_home", "fake_endpoint_id")
class TestSubagentRouting:
    def test_evaluates_subagent_transcript_not_main(
        self, transcripts: TranscriptWriter, fake_ai_guard
    ) -> None:
        transcripts.write_main(SESSION, [user_text("MAIN conversation")])
        path = transcripts.write_subagent(
            SESSION,
            "a1b2",
            [user_text("subagent task"), assistant_text("on it")],
        )

        result = _invoke("PreToolUse", _pre_tool_event(path, agent_id="a1b2"))

        assert result.exit_code == 0, result.output
        rendered = json.dumps(fake_ai_guard.last_messages)
        assert "subagent task" in rendered
        assert "MAIN conversation" not in rendered


@pytest.mark.usefixtures("tmp_home", "fake_endpoint_id")
class TestGracefulBehavior:
    def test_session_start_is_noop(self, fake_ai_guard) -> None:
        result = _invoke("SessionStart", {"session_id": SESSION})
        assert result.exit_code == 0
        assert result.output == ""
        assert fake_ai_guard.calls == []  # lifecycle hooks don't evaluate

    def test_unknown_agent_is_noop(self) -> None:
        result = CliRunner().invoke(hook, ["bogus", "PreToolUse"], input=b"{}")
        assert result.exit_code == 0
        assert result.output == ""

    def test_invalid_json_payload_is_tolerated(self, fake_ai_guard) -> None:
        result = CliRunner().invoke(hook, ["claude", "PreToolUse"], input=b"{not json")
        assert result.exit_code == 0
        # Empty event → no transcript_path → nothing to evaluate.
        assert fake_ai_guard.calls == []

    def test_missing_transcript_still_evaluates_pending_call(self, fake_ai_guard) -> None:
        # A missing/unflushed transcript must not skip the check — the pending
        # call is still evaluated, built from the event.
        event = _pre_tool_event("/nonexistent/path.jsonl")
        result = _invoke("PreToolUse", event)
        assert result.exit_code == 0
        assert len(fake_ai_guard.calls) == 1
        assert fake_ai_guard.last_messages[-1]["tool_calls"][0]["function"]["name"] == "Bash"
