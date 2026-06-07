# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Unit tests for ``aiguard.claude.handler``.

Grouped into classes by what's being tested:

  * :class:`TestBlockedTool` — ``_blocked_tool_response`` payload shaping.
  * :class:`TestAIGuardUIURL` — ``_ai_guard_ui_url`` investigate-link building.
  * :class:`TestLoadMessages` — rebuilding history from a transcript.
  * :class:`TestResolveTranscript` — main vs. subagent transcript selection.
  * :class:`TestAppendToolResult` — the PostToolUse de-dupe helper.
  * :class:`TestHandleHookSpans` — span-emitting lifecycle hooks.
  * :class:`TestHandleHookToolUse` — Pre/Post tool hooks evaluate + shape output.
  * :class:`TestHandleHookTags` — user-id tagging (user@host).
  * :class:`TestHandleHookDispatch` — payload tolerance + camelCase dispatch.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from pathlib import Path
from typing import Any

import pytest
from ddtrace.appsec.ai_guard import AIGuardAbortError

from aiguard.claude.handler import (
    ClaudeHandler,
    _ai_guard_ui_url,
    _append_tool_result,
    _blocked_tool_response,
    _entry_to_messages,
    _load_messages,
    _privacy_mode,
    _resolve_transcript,
)
from aiguard.constants import AIGuardConstants
from tests.transcripts import (
    TranscriptWriter,
    assistant_text,
    assistant_tool_use,
    tool_result,
    user_text,
)


def _handler() -> ClaudeHandler:
    return ClaudeHandler(blocking=True)


class TestPrivacyMode:
    """``_privacy_mode`` resolves DD_AI_GUARD_PRIVACY_MODE for the client."""

    def test_defaults_to_coding_agent_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(AIGuardConstants.PRIVACY_MODE_ENV, raising=False)
        assert _privacy_mode() == AIGuardConstants.PRIVACY_MODE_CODING_AGENT

    def test_honours_default_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(AIGuardConstants.PRIVACY_MODE_ENV, "DEFAULT")
        assert _privacy_mode() == AIGuardConstants.PRIVACY_MODE_DEFAULT

    def test_is_case_insensitive_and_trims(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(AIGuardConstants.PRIVACY_MODE_ENV, "  default ")
        assert _privacy_mode() == AIGuardConstants.PRIVACY_MODE_DEFAULT

    def test_unknown_value_falls_back_to_coding_agent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(AIGuardConstants.PRIVACY_MODE_ENV, "bogus")
        assert _privacy_mode() == AIGuardConstants.PRIVACY_MODE_CODING_AGENT


def _pre_tool_payload(transcript_path: str, **extra: Any) -> bytes:
    event = {
        "hook_event_name": "PreToolUse",
        "session_id": "s1",
        "transcript_path": transcript_path,
        "tool_use_id": "tu1",
        "tool_name": "Bash",
    }
    event.update(extra)
    return json.dumps(event).encode()


# ── _blocked_tool_response ─────────────────────────────────────────────────────


class TestBlockedTool:
    def test_pre_tool_use_denies_with_branded_display_reason(self) -> None:
        abort = AIGuardAbortError(action="DENY", reason="prompt_injection", tags=["t"])
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash"}
        result = _blocked_tool_response(event, abort)
        specific = result["hookSpecificOutput"]
        assert specific["hookEventName"] == "PreToolUse"
        assert specific["permissionDecision"] == "deny"
        assert "Datadog AI Guard" in specific["permissionDecisionReason"]
        assert "Bash" in specific["additionalContext"]
        assert "prompt_injection" in specific["additionalContext"]
        assert "decision" not in result

    def test_post_tool_use_carries_context_alongside_reason(self) -> None:
        abort = AIGuardAbortError(action="DENY", reason="prompt_injection", tags=["t"])
        event = {"hook_event_name": "PostToolUse", "tool_name": "Bash"}
        result = _blocked_tool_response(event, abort)
        assert result["decision"] == "block"
        assert "Datadog AI Guard" in result["reason"]
        specific = result["hookSpecificOutput"]
        assert specific["hookEventName"] == "PostToolUse"
        assert "Bash" in specific["additionalContext"]
        assert "prompt_injection" in specific["additionalContext"]

    def test_includes_tag_probs_breakdown_for_model(self) -> None:
        abort = AIGuardAbortError(
            action="DENY",
            reason="prompt_injection",
            tags=["t"],
            tag_probs={"prompt_injection": 0.92, "secrets_exfiltration": 0.08},
        )
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash"}
        context = _blocked_tool_response(event, abort)["hookSpecificOutput"]["additionalContext"]
        assert "prompt_injection" in context
        assert "92%" in context
        assert context.index("prompt_injection") < context.index("secrets_exfiltration")
        assert "confidence as a percentage" in context

    def test_skill_block_omits_path_when_folder_missing(self) -> None:
        abort = AIGuardAbortError(action="DENY", reason="malicious_skill", tags=["t"])
        event = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Skill",
            "tool_input": {"skill": "unknown-skill"},
            "cwd": "/tmp",
        }
        context = _blocked_tool_response(event, abort)["hookSpecificOutput"]["additionalContext"]
        assert "located at" not in context
        assert "audit any other recently installed skills" in context

    def test_pre_tool_use_includes_ui_url_when_session_id_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DD_SITE", raising=False)
        abort = AIGuardAbortError(action="DENY", reason="prompt_injection", tags=["t"])
        sid = "01e28aae-7b00-43e8-af0e-b5e6b3b9c7ed"
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": sid}
        context = _blocked_tool_response(event, abort)["hookSpecificOutput"]["additionalContext"]
        url_prefix = "https://app.datadoghq.com/security/ai-guard/investigate?query="
        assert f"- Investigate in Datadog: {url_prefix}" in context
        assert sid in context
        assert "include it in the response" in context

    def test_omits_ui_url_when_session_id_missing(self) -> None:
        abort = AIGuardAbortError(action="DENY", reason="r", tags=["t"])
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash"}
        context = _blocked_tool_response(event, abort)["hookSpecificOutput"]["additionalContext"]
        assert "/security/ai-guard/investigate" not in context
        assert "Investigate in Datadog" not in context


class TestAIGuardUIURL:
    """``_ai_guard_ui_url`` builds the Datadog investigate link or returns None."""

    def test_returns_none_when_session_id_is_empty(self) -> None:
        assert _ai_guard_ui_url("") is None

    def test_defaults_to_datadoghq_when_dd_site_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DD_SITE", raising=False)
        url = _ai_guard_ui_url("sess-42")
        assert url is not None
        assert url.startswith("https://app.datadoghq.com/security/ai-guard/investigate?query=")

    def test_regional_site_skips_app_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DD_SITE", "us3.datadoghq.com")
        url = _ai_guard_ui_url("sess-42")
        assert url is not None
        assert url.startswith("https://us3.datadoghq.com/security/ai-guard/investigate?query=")

    @pytest.mark.parametrize(
        "site",
        ["datadoghq.com", "datadoghq.eu", "ddog-gov.com", "datad0g.com"],
    )
    def test_app_prefix_applied_for_non_regional_sites(
        self, monkeypatch: pytest.MonkeyPatch, site: str
    ) -> None:
        monkeypatch.setenv("DD_SITE", site)
        url = _ai_guard_ui_url("sess-42")
        assert url is not None
        assert url.startswith(f"https://app.{site}/security/ai-guard/investigate?query=")

    def test_query_filters_by_resource_coding_agent_and_session_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DD_SITE", raising=False)
        sid = "01e28aae-7b00-43e8-af0e-b5e6b3b9c7ed"
        url = _ai_guard_ui_url(sid)
        assert url is not None
        _, _, query = url.partition("?query=")
        decoded = urllib.parse.unquote(query)
        assert "resource_name:ai_guard" in decoded
        assert "@ai_guard.coding_agent:*" in decoded
        assert f"@ai_guard.usr.session_id:{sid}" in decoded


# ── _load_messages: transcript → AI Guard messages ─────────────────────────────


class TestLoadMessages:
    def test_returns_empty_for_blank_or_missing_path(self, tmp_path: Path) -> None:
        assert _load_messages("", "") == []
        assert _load_messages(str(tmp_path / "nope.jsonl"), "") == []

    def test_user_string_content_passes_through(self, transcripts: TranscriptWriter) -> None:
        path = transcripts.write_main("s", [user_text("hello there")])
        assert _load_messages(path, "") == [{"role": "user", "content": "hello there"}]

    def test_assistant_text_becomes_content_parts(self, transcripts: TranscriptWriter) -> None:
        path = transcripts.write_main("s", [assistant_text("sure")])
        messages = _load_messages(path, "")
        assert messages[0]["role"] == "assistant"
        assert messages[0]["content"][0]["type"] == "text"
        assert messages[0]["content"][0]["text"] == "sure"
        assert "tool_calls" not in messages[0]

    def test_assistant_tool_use_becomes_tool_calls(self, transcripts: TranscriptWriter) -> None:
        path = transcripts.write_main("s", [assistant_tool_use("tu1", "Read", {"path": "x"})])
        msg = _load_messages(path, "")[0]
        assert msg["role"] == "assistant"
        assert "content" not in msg
        assert msg["tool_calls"][0]["id"] == "tu1"
        assert msg["tool_calls"][0]["function"]["name"] == "Read"
        assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"path": "x"}

    def test_user_tool_result_becomes_tool_role_message(
        self, transcripts: TranscriptWriter
    ) -> None:
        path = transcripts.write_main("s", [tool_result("tu7", "OK")])
        messages = _load_messages(path, "")
        assert messages[0]["role"] == "tool"
        assert messages[0]["tool_call_id"] == "tu7"
        assert messages[0]["content"] == "OK"

    def test_full_conversation_order_preserved(self, transcripts: TranscriptWriter) -> None:
        path = transcripts.write_main(
            "s",
            [
                user_text("run ls"),
                assistant_tool_use("tu1", "Bash", {"command": "ls"}),
                tool_result("tu1", "a\nb"),
                assistant_text("done"),
            ],
        )
        roles = [m["role"] for m in _load_messages(path, "")]
        assert roles == ["user", "assistant", "tool", "assistant"]

    def test_metadata_rows_are_ignored(self, transcripts: TranscriptWriter) -> None:
        path = transcripts.write_main(
            "s",
            [
                {"type": "file-history-snapshot", "snapshot": {}},
                {"type": "mode", "mode": "default"},
                user_text("only real turn"),
            ],
        )
        messages = _load_messages(path, "")
        assert messages == [{"role": "user", "content": "only real turn"}]

    def test_malformed_lines_are_skipped(self, transcripts: TranscriptWriter) -> None:
        good = json.dumps(user_text("survivor"))
        path = transcripts.write_raw("s", f"{good}\n{{not json\n\n{good}\n")
        messages = _load_messages(path, "")
        assert len(messages) == 2
        assert all(m["content"] == "survivor" for m in messages)


class TestResolveTranscript:
    def test_main_session_uses_transcript_path(self, transcripts: TranscriptWriter) -> None:
        path = transcripts.write_main("s", [user_text("hi")])
        assert _resolve_transcript(path, "") == Path(path)

    def test_subagent_prefers_subagent_file(self, transcripts: TranscriptWriter) -> None:
        transcripts.write_main("s", [user_text("main")])
        main = transcripts.write_subagent("s", "a1", [user_text("sub")])
        resolved = _resolve_transcript(main, "a1")
        assert resolved is not None
        assert resolved.name == "agent-a1.jsonl"
        assert "subagents" in resolved.parts

    def test_subagent_falls_back_to_main_when_missing(self, transcripts: TranscriptWriter) -> None:
        main = transcripts.write_main("s", [user_text("main")])
        assert _resolve_transcript(main, "missing-agent") == Path(main)

    def test_load_messages_routes_to_subagent(self, transcripts: TranscriptWriter) -> None:
        transcripts.write_main("s", [user_text("MAIN")])
        main = transcripts.write_subagent("s", "a1", [user_text("SUB")])
        messages = _load_messages(main, "a1")
        assert messages == [{"role": "user", "content": "SUB"}]


class TestEntryToMessages:
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
        msg = _entry_to_messages(entry)[0]
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
        roles = [m["role"] for m in _entry_to_messages(entry)]
        assert roles == ["tool", "user"]

    def test_non_conversation_entry_returns_empty(self) -> None:
        assert _entry_to_messages({"type": "summary"}) == []
        assert _entry_to_messages({"type": "user", "message": "not a dict"}) == []


class TestAppendToolResult:
    def test_appends_when_absent(self) -> None:
        messages = [{"role": "assistant", "tool_calls": [{"id": "t1"}]}]
        _append_tool_result(messages, "t1", "output")
        assert messages[-1] == {"role": "tool", "tool_call_id": "t1", "content": "output"}

    def test_skips_when_transcript_already_has_result(self) -> None:
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "t1"}]},
            {"role": "tool", "tool_call_id": "t1", "content": "already there"},
        ]
        _append_tool_result(messages, "t1", "from hook")
        assert len(messages) == 2
        assert messages[-1]["content"] == "already there"


# ── handle_hook dispatch ───────────────────────────────────────────────────────


class TestHandleHookSpans:
    """Lifecycle hooks emit a span tagged with the event metadata."""

    def test_session_start(self, tracer_recorder, tmp_home: Path) -> None:
        out = _handler().handle_hook(
            "SessionStart",
            json.dumps({"session_id": "s1", "model": "claude-sonnet-4-5"}).encode(),
        )
        assert out == b""
        span = tracer_recorder.spans[0]
        assert span.name == AIGuardConstants.SESSION_START
        assert span.resource == AIGuardConstants.HOOK_RESOURCE
        assert span.tags[AIGuardConstants.SESSION_ID_TAG] == "s1"
        assert span.tags[AIGuardConstants.MODEL_TAG] == "claude-sonnet-4-5"
        assert span.tags[AIGuardConstants.CODING_AGENT_TAG] == AIGuardConstants.CLAUDE_CODE

    def test_session_end(self, tracer_recorder, tmp_home: Path) -> None:
        _handler().handle_hook(
            "SessionEnd", json.dumps({"session_id": "sX", "reason": "logout"}).encode()
        )
        assert tracer_recorder.spans[0].name == AIGuardConstants.SESSION_END
        assert tracer_recorder.spans[0].tags[AIGuardConstants.SESSION_ID_TAG] == "sX"

    def test_subagent_start(self, tracer_recorder, tmp_home: Path) -> None:
        _handler().handle_hook(
            "SubagentStart",
            json.dumps({"session_id": "s1", "agent_id": "a7", "agent_type": "general"}).encode(),
        )
        span = tracer_recorder.spans[0]
        assert span.name == AIGuardConstants.SUBAGENT_START
        assert span.tags[AIGuardConstants.SESSION_ID_TAG] == "s1"
        assert span.tags[AIGuardConstants.SUBAGENT_ID_TAG] == "a7"
        assert span.tags[AIGuardConstants.SUBAGENT_TYPE_TAG] == "general"

    def test_subagent_stop(self, tracer_recorder, tmp_home: Path) -> None:
        _handler().handle_hook(
            "SubagentStop",
            json.dumps({"session_id": "s1", "agent_id": "a8", "agent_type": "explorer"}).encode(),
        )
        span = tracer_recorder.spans[0]
        assert span.name == AIGuardConstants.SUBAGENT_STOP
        assert span.tags[AIGuardConstants.SUBAGENT_ID_TAG] == "a8"
        assert span.tags[AIGuardConstants.SUBAGENT_TYPE_TAG] == "explorer"


class TestHandleHookToolUse:
    """Pre/Post tool hooks rebuild history from the transcript, evaluate, and
    shape the response."""

    def test_pre_tool_use_evaluates_pending_tool_call(
        self, tracer_recorder, tmp_home: Path, transcripts: TranscriptWriter, fake_ai_guard
    ) -> None:
        path = transcripts.write_main(
            "s-pre",
            [
                user_text("please run something"),
                assistant_tool_use("tu1", "Bash", {"command": "ls"}),
            ],
        )
        out = _handler().handle_hook("PreToolUse", _pre_tool_payload(path, session_id="s-pre"))

        assert out == b""
        assert tracer_recorder.spans[0].name == AIGuardConstants.PRE_TOOL
        assert len(fake_ai_guard.calls) == 1
        last = fake_ai_guard.last_messages[-1]
        assert last["role"] == "assistant"
        assert last["tool_calls"][0]["function"]["name"] == "Bash"

    def test_pre_tool_use_injects_skill_markdown_as_tool_message(
        self, tracer_recorder, tmp_home: Path, transcripts: TranscriptWriter, fake_ai_guard
    ) -> None:
        skill_dir = tmp_home / ".claude" / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# my-skill body")

        path = transcripts.write_main("s-skill", [user_text("run a skill")])
        out = _handler().handle_hook(
            "PreToolUse",
            _pre_tool_payload(
                path,
                session_id="s-skill",
                cwd=str(tmp_home),
                tool_name="Skill",
                tool_input={"skill": "my-skill"},
            ),
        )

        assert out == b""
        last = fake_ai_guard.last_messages[-1]
        assert last["role"] == "tool"
        assert last["tool_call_id"] == "tu1"
        assert "my-skill body" in last["content"]

    def test_pre_tool_use_skill_lookup_honours_claude_config_dir(
        self,
        tracer_recorder,
        tmp_home: Path,
        transcripts: TranscriptWriter,
        fake_ai_guard,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        override = tmp_home / "work-claude"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(override))
        skill_dir = override / "skills" / "scoped-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# scoped-skill body")

        decoy = tmp_home / ".claude" / "skills" / "scoped-skill"
        decoy.mkdir(parents=True)
        (decoy / "SKILL.md").write_text("# decoy body")

        path = transcripts.write_main("s-override", [user_text("run a skill")])
        out = _handler().handle_hook(
            "PreToolUse",
            _pre_tool_payload(
                path,
                session_id="s-override",
                cwd="/",  # outside tmp_home so the project-walk branch finds nothing
                tool_name="Skill",
                tool_input={"skill": "scoped-skill"},
            ),
        )

        assert out == b""
        last = fake_ai_guard.last_messages[-1]
        assert "scoped-skill body" in last["content"]
        assert "decoy body" not in last["content"]

    def test_pre_tool_use_returns_deny_payload_when_ai_guard_aborts(
        self, tracer_recorder, tmp_home: Path, transcripts: TranscriptWriter, fake_ai_guard
    ) -> None:
        path = transcripts.write_main(
            "s-deny", [assistant_tool_use("tu1", "Bash", {"command": "rm -rf /"})]
        )
        fake_ai_guard.queue_abort(
            AIGuardAbortError(action="DENY", reason="prompt_injection", tags=["t"])
        )
        out = _handler().handle_hook("PreToolUse", _pre_tool_payload(path, session_id="s-deny"))

        body = json.loads(out)
        hook_out = body["hookSpecificOutput"]
        assert hook_out["hookEventName"] == "PreToolUse"
        assert hook_out["permissionDecision"] == "deny"
        assert "Datadog AI Guard" in hook_out["permissionDecisionReason"]
        assert "prompt_injection" in hook_out["additionalContext"]

    def test_post_tool_use_appends_tool_message_and_evaluates(
        self, tracer_recorder, tmp_home: Path, transcripts: TranscriptWriter, fake_ai_guard
    ) -> None:
        path = transcripts.write_main("s-post", [assistant_tool_use("tu1", "Read", {"path": "x"})])
        out = _handler().handle_hook(
            "PostToolUse",
            json.dumps(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": "s-post",
                    "transcript_path": path,
                    "tool_use_id": "tu1",
                    "tool_name": "Read",
                    "tool_response": "file contents",
                }
            ).encode(),
        )

        assert out == b""
        assert tracer_recorder.spans[0].name == AIGuardConstants.POST_TOOL
        last = fake_ai_guard.last_messages[-1]
        assert last["role"] == "tool"
        assert last["tool_call_id"] == "tu1"
        assert last["content"] == "file contents"

    def test_post_tool_use_returns_block_payload_when_ai_guard_aborts(
        self, tracer_recorder, tmp_home: Path, transcripts: TranscriptWriter, fake_ai_guard
    ) -> None:
        path = transcripts.write_main("s-post", [assistant_tool_use("tu1", "Read", {})])
        fake_ai_guard.queue_abort(
            AIGuardAbortError(action="DENY", reason="leaked_secret", tags=["t"])
        )
        out = _handler().handle_hook(
            "PostToolUse",
            json.dumps(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": "s-post",
                    "transcript_path": path,
                    "tool_use_id": "tu1",
                    "tool_name": "Read",
                    "tool_response": "ssh-rsa AAAA...",
                }
            ).encode(),
        )

        body = json.loads(out)
        assert body["decision"] == "block"
        assert "Datadog AI Guard" in body["reason"]
        assert "leaked_secret" in body["hookSpecificOutput"]["additionalContext"]

    def test_post_tool_use_failure_appends_tool_error_message(
        self, tracer_recorder, tmp_home: Path, transcripts: TranscriptWriter, fake_ai_guard
    ) -> None:
        path = transcripts.write_main("s-fail", [assistant_tool_use("tu1", "Bash", {})])
        out = _handler().handle_hook(
            "PostToolUseFailure",
            json.dumps(
                {
                    "hook_event_name": "PostToolUseFailure",
                    "session_id": "s-fail",
                    "transcript_path": path,
                    "tool_use_id": "tu1",
                    "error": "permission denied",
                }
            ).encode(),
        )

        assert out == b""
        last = fake_ai_guard.last_messages[-1]
        assert last["role"] == "tool"
        assert last["content"] == "permission denied"

    def test_pre_tool_use_evaluates_subagent_transcript_not_main(
        self, tracer_recorder, tmp_home: Path, transcripts: TranscriptWriter, fake_ai_guard
    ) -> None:
        transcripts.write_main("s-mix", [user_text("parent: draft an email")])
        path = transcripts.write_subagent(
            "s-mix",
            "a7",
            [
                user_text("sub: read README"),
                assistant_tool_use("tu1", "Read", {"path": "README.md"}),
            ],
        )

        _handler().handle_hook(
            "PreToolUse",
            _pre_tool_payload(
                path,
                session_id="s-mix",
                agent_id="a7",
                tool_name="Read",
                tool_input={"path": "README.md"},
            ),
        )

        messages = fake_ai_guard.last_messages
        user_msgs = [m for m in messages if m.get("role") == "user"]
        assert user_msgs[0]["content"] == "sub: read README"
        assert all("parent:" not in str(m.get("content", "")) for m in messages)


class TestHandleHookTags:
    """Span tagging picks up the ``user@host`` id from ``utils.fetch_endpoint_id``."""

    def test_tags_user_id_from_fetch_endpoint_id(
        self, tracer_recorder, fake_endpoint_id: str, tmp_home: Path
    ) -> None:
        _handler().handle_hook("SessionStart", json.dumps({"session_id": "s1"}).encode())
        assert tracer_recorder.spans[0].tags[AIGuardConstants.USER_ID_TAG] == fake_endpoint_id


class TestHandleHookDispatch:
    """Payload tolerance + camelCase → snake_case method dispatch."""

    def test_dispatch_logs_at_debug(
        self, tracer_recorder, tmp_home: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """At DEBUG the hook emits a visible dispatch + verdict line.

        Without this the happy path is silent, so DD_AI_GUARD_LOG_LEVEL=DEBUG
        looks like it has no effect.
        """
        with caplog.at_level(logging.DEBUG, logger="ai_guard"):
            _handler().handle_hook("SessionStart", json.dumps({"session_id": "s1"}).encode())
        messages = [r.message for r in caplog.records]
        assert any("dispatching hook SessionStart" in m for m in messages)
        assert any("-> allow" in m for m in messages)

    def test_tolerates_invalid_json(self, tracer_recorder, tmp_home: Path) -> None:
        out = _handler().handle_hook("SessionStart", b"{not json")
        assert out == b""
        span = tracer_recorder.spans[0]
        assert span.name == AIGuardConstants.SESSION_START
        assert span.tags[AIGuardConstants.SESSION_ID_TAG] == ""

    def test_tolerates_empty_payload(self, tracer_recorder, tmp_home: Path) -> None:
        out = _handler().handle_hook("SessionStart", b"")
        assert out == b""
        assert tracer_recorder.spans[0].name == AIGuardConstants.SESSION_START

    def test_unknown_hook_emits_warning_no_span(
        self, tracer_recorder, tmp_home: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="ai_guard"):
            out = _handler().handle_hook("NotAHook", b"{}")
        assert out == b""
        assert tracer_recorder.spans == []
        assert any("unhandled hook" in rec.message for rec in caplog.records)

    def test_serializes_dict_returned_by_method(self, tracer_recorder, tmp_home: Path) -> None:
        class _Echo(ClaudeHandler):
            def _echo(self, event: dict[str, Any]) -> dict[str, Any]:
                return {"received": event}

        out = _Echo(blocking=True).handle_hook("Echo", json.dumps({"a": 1}).encode())
        assert json.loads(out) == {"received": {"a": 1}}

    def test_translates_camel_case_to_snake_case(self, tmp_home: Path) -> None:
        captured: dict = {}

        class _Sub(ClaudeHandler):
            def _my_custom_hook(self, event: dict[str, Any]) -> None:
                captured["seen"] = event
                return None

        _Sub(blocking=True).handle_hook("MyCustomHook", json.dumps({"x": 1}).encode())
        assert captured["seen"] == {"x": 1}
