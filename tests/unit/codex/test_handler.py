# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Unit tests for the Codex hook handler."""

from __future__ import annotations

import json
from typing import Any

import pytest

from aiguard.client import AIGuardAbortError
from aiguard.codex.handler import (
    CodexHandler,
    _append_pending_tool_call,
    _fetch_email,
    _load_messages,
)
from aiguard.constants import AIGuardConstants
from tests.conftest import FakeAIGuardClient, TracerRecorder
from tests.transcripts import (
    CodexRolloutWriter,
    codex_assistant_message,
    codex_function_call,
    codex_user_message,
)

SESSION = "sess-codex-1"


def _handler() -> CodexHandler:
    return CodexHandler(blocking=True)


def _pre_tool_payload(transcript_path: str | None, **extra: Any) -> bytes:
    event = {
        "hook_event_name": "PreToolUse",
        "session_id": SESSION,
        "transcript_path": transcript_path,
        "tool_name": "shell",
        "tool_use_id": "call_1",
        "tool_input": {"command": ["bash", "-lc", "ls"]},
    }
    event.update(extra)
    return json.dumps(event).encode()


# ── dispatch ────────────────────────────────────────────────────────────────


class TestDispatch:
    def test_unknown_hook_is_noop(self) -> None:
        assert _handler().handle_hook("NopeEvent", b"{}") == b""

    def test_garbage_payload_is_tolerated(self) -> None:
        # Invalid JSON → empty event → handler still allows.
        assert _handler().handle_hook("SessionStart", b"not json") == b""

    def test_camel_to_snake_dispatch(self, tracer_recorder: TracerRecorder) -> None:
        _handler().handle_hook("PreToolUse", _pre_tool_payload(None))
        assert any(s.name == AIGuardConstants.PRE_TOOL for s in tracer_recorder.spans)


# ── lifecycle spans ───────────────────────────────────────────────────────────


class TestLifecycleSpans:
    @pytest.mark.parametrize(
        "hook,op",
        [
            ("SessionStart", AIGuardConstants.SESSION_START),
            ("SubagentStart", AIGuardConstants.SUBAGENT_START),
            ("SubagentStop", AIGuardConstants.SUBAGENT_STOP),
            ("Stop", AIGuardConstants.STOP),
        ],
    )
    def test_emits_tagged_span(
        self, hook: str, op: str, tracer_recorder: TracerRecorder, fake_endpoint_id: str
    ) -> None:
        event = {"session_id": SESSION, "model": "gpt-5-codex"}
        result = _handler().handle_hook(hook, json.dumps(event).encode())
        assert result == b""
        span = next(s for s in tracer_recorder.spans if s.name == op)
        assert span.tags[AIGuardConstants.CODING_AGENT_TAG] == AIGuardConstants.CODEX_CLI
        assert span.tags[AIGuardConstants.SESSION_ID_TAG] == SESSION
        assert span.tags[AIGuardConstants.MODEL_TAG] == "gpt-5-codex"


# ── PreToolUse ────────────────────────────────────────────────────────────────


class TestPreToolUse:
    def test_allow_returns_empty(
        self, fake_ai_guard: FakeAIGuardClient, tracer_recorder: TracerRecorder
    ) -> None:
        assert _handler().handle_hook("PreToolUse", _pre_tool_payload(None)) == b""
        assert fake_ai_guard.calls, "AI Guard should have been consulted"

    def test_pending_call_evaluated_without_transcript(
        self, fake_ai_guard: FakeAIGuardClient, tracer_recorder: TracerRecorder
    ) -> None:
        _handler().handle_hook("PreToolUse", _pre_tool_payload(None))
        msgs = fake_ai_guard.last_messages
        # Only the pending assistant tool call is present.
        assert msgs[-1]["role"] == "assistant"
        assert msgs[-1]["tool_calls"][0]["id"] == "call_1"
        assert msgs[-1]["tool_calls"][0]["function"]["name"] == "shell"

    def test_deny_payload_shape(
        self, fake_ai_guard: FakeAIGuardClient, tracer_recorder: TracerRecorder
    ) -> None:
        fake_ai_guard.queue_abort(
            AIGuardAbortError(
                action="DENY",
                reason="prompt injection",
                tags=["prompt_injection"],
                tag_probs={"prompt_injection": 0.9},
            )
        )
        out = _handler().handle_hook("PreToolUse", _pre_tool_payload(None))
        result = json.loads(out)
        hso = result["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert hso["permissionDecision"] == "deny"
        assert "Datadog AI Guard" in hso["permissionDecisionReason"]
        assert "prompt injection" in hso["additionalContext"]

    def test_transcript_history_loaded(
        self,
        codex_transcripts: CodexRolloutWriter,
        fake_ai_guard: FakeAIGuardClient,
        tracer_recorder: TracerRecorder,
    ) -> None:
        path = codex_transcripts.write(
            SESSION, [codex_user_message("ls please"), codex_assistant_message("sure")]
        )
        _handler().handle_hook("PreToolUse", _pre_tool_payload(path))
        roles = [m["role"] for m in fake_ai_guard.last_messages]
        assert roles == ["user", "assistant", "assistant"]  # history + pending call

    def test_pending_call_deduped_against_flushed(
        self,
        codex_transcripts: CodexRolloutWriter,
        fake_ai_guard: FakeAIGuardClient,
        tracer_recorder: TracerRecorder,
    ) -> None:
        # The rollout already carries the function_call with the same call_id.
        path = codex_transcripts.write(
            SESSION,
            [codex_function_call("call_1", "shell", '{"command":["bash","-lc","ls"]}')],
        )
        _handler().handle_hook("PreToolUse", _pre_tool_payload(path))
        tool_call_ids = [
            tc["id"]
            for m in fake_ai_guard.last_messages
            if m["role"] == "assistant"
            for tc in m.get("tool_calls", [])
        ]
        assert tool_call_ids == ["call_1"], "pending call must not be duplicated"


# ── PostToolUse ───────────────────────────────────────────────────────────────


class TestPostToolUse:
    def _post_payload(self, **extra: Any) -> bytes:
        event = {
            "hook_event_name": "PostToolUse",
            "session_id": SESSION,
            "transcript_path": None,
            "tool_name": "shell",
            "tool_use_id": "call_1",
            "tool_response": "command output",
        }
        event.update(extra)
        return json.dumps(event).encode()

    def test_appends_tool_result(
        self, fake_ai_guard: FakeAIGuardClient, tracer_recorder: TracerRecorder
    ) -> None:
        _handler().handle_hook("PostToolUse", self._post_payload())
        last = fake_ai_guard.last_messages[-1]
        assert last == {"role": "tool", "tool_call_id": "call_1", "content": "command output"}

    def test_block_payload_shape(
        self, fake_ai_guard: FakeAIGuardClient, tracer_recorder: TracerRecorder
    ) -> None:
        fake_ai_guard.queue_abort(
            AIGuardAbortError(action="DENY", reason="secret exfiltration", tags=["secret"])
        )
        out = _handler().handle_hook("PostToolUse", self._post_payload())
        result = json.loads(out)
        assert result["decision"] == "block"
        assert "Datadog AI Guard" in result["reason"]
        assert result["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


# ── UserPromptSubmit ──────────────────────────────────────────────────────────


class TestUserPromptSubmit:
    def _payload(self, prompt: str, **extra: Any) -> bytes:
        event = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": SESSION,
            "transcript_path": None,
            "prompt": prompt,
        }
        event.update(extra)
        return json.dumps(event).encode()

    def test_evaluates_prompt_text(
        self, fake_ai_guard: FakeAIGuardClient, tracer_recorder: TracerRecorder
    ) -> None:
        assert _handler().handle_hook("UserPromptSubmit", self._payload("hello")) == b""
        assert fake_ai_guard.last_messages[0] == {"role": "user", "content": "hello"}

    def test_blocked_prompt_reason_shape(
        self, fake_ai_guard: FakeAIGuardClient, tracer_recorder: TracerRecorder
    ) -> None:
        fake_ai_guard.queue_abort(
            AIGuardAbortError(
                action="DENY",
                reason="prompt injection",
                tags=["prompt_injection"],
                tag_probs={"prompt_injection": 0.88},
            )
        )
        out = _handler().handle_hook("UserPromptSubmit", self._payload("do evil"))
        result = json.loads(out)
        assert result["decision"] == "block"
        assert "Datadog AI Guard" in result["reason"]
        assert "prompt injection" in result["reason"]
        assert result["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"

    def test_explicit_skill_resolved_and_injected(
        self, tmp_home: Any, fake_ai_guard: FakeAIGuardClient, tracer_recorder: TracerRecorder
    ) -> None:
        skill_dir = tmp_home / ".agents" / "skills" / "deploy"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("malicious skill body", encoding="utf-8")
        _handler().handle_hook("UserPromptSubmit", self._payload("please run $deploy now"))
        msgs = fake_ai_guard.last_messages
        # user prompt + modelled skill tool call + its result
        assert any(
            m["role"] == "tool" and m.get("content") == "malicious skill body" for m in msgs
        )
        assert any(
            m["role"] == "assistant"
            and any(tc["function"]["name"] == "skill" for tc in m.get("tool_calls", []))
            for m in msgs
        )

    def test_custom_prompt_resolved_and_injected(
        self, tmp_home: Any, fake_ai_guard: FakeAIGuardClient, tracer_recorder: TracerRecorder
    ) -> None:
        prompts_dir = tmp_home / ".codex" / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        (prompts_dir / "review.md").write_text("review prompt body", encoding="utf-8")
        _handler().handle_hook("UserPromptSubmit", self._payload("/prompts:review"))
        msgs = fake_ai_guard.last_messages
        assert any(
            m["role"] == "tool" and m.get("content") == "review prompt body" for m in msgs
        )

    def test_unresolved_reference_injects_nothing(
        self, tmp_home: Any, fake_ai_guard: FakeAIGuardClient, tracer_recorder: TracerRecorder
    ) -> None:
        _handler().handle_hook("UserPromptSubmit", self._payload("use $nonexistent"))
        # Only the prompt itself is evaluated.
        assert fake_ai_guard.last_messages == [{"role": "user", "content": "use $nonexistent"}]


# ── helpers ───────────────────────────────────────────────────────────────────


class TestHelpers:
    def test_load_messages_none_path(self) -> None:
        assert _load_messages(None) == []

    def test_load_messages_missing_file(self, tmp_path: Any) -> None:
        assert _load_messages(str(tmp_path / "nope.jsonl")) == []

    def test_load_messages_tolerates_malformed(
        self, codex_transcripts: CodexRolloutWriter
    ) -> None:
        path = codex_transcripts.write_raw(
            SESSION,
            '{"type":"response_item","payload":{"type":"message","role":"user",'
            '"content":[{"type":"input_text","text":"ok"}]}}\nnot json\n\n',
        )
        msgs = _load_messages(path)
        assert msgs == [{"role": "user", "content": [{"type": "text", "text": "ok"}]}]

    def test_append_pending_skips_when_no_tool_name(self) -> None:
        msgs: list = []
        _append_pending_tool_call(msgs, {"tool_use_id": "x"})
        assert msgs == []

    def test_fetch_email_from_auth_json(self, tmp_home: Any) -> None:
        import base64

        codex_dir = tmp_home / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        payload = base64.urlsafe_b64encode(b'{"email":"dev@example.com"}').decode().rstrip("=")
        token = f"hdr.{payload}.sig"
        (codex_dir / "auth.json").write_text(
            json.dumps({"tokens": {"id_token": token}}), encoding="utf-8"
        )
        assert _fetch_email() == "dev@example.com"

    def test_fetch_email_missing_auth(self, tmp_home: Any) -> None:
        assert _fetch_email() is None
