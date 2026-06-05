# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Unit tests for everything under ``aiguard.claude``.

Grouped into classes by what's being tested:

  * :class:`TestClaudeProxyMatches` — ``ClaudeProxy.matches`` (the routing
    key) and the rest of the ``ProxyHandler`` surface (``agent``,
    ``upstream``).
  * :class:`TestBlockedTool` — ``_blocked_tool_response``.
  * :class:`TestRequestParser` — ``_parse_request_body``.
  * :class:`TestAnthropicMessageParser` — ``_parse_anthropic_message``.
  * :class:`TestSessionKeysParser` — ``_fetch_session_keys``.
  * :class:`TestSSEResponseParser` — ``_parse_sse_body``.
  * :class:`TestHandleHookSpans` — span-emitting hooks (Session/Subagent).
  * :class:`TestHandleHookToolUse` — Pre/Post tool hooks.
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
from aiohttp.test_utils import make_mocked_request
from ddtrace.appsec.ai_guard import AIGuardAbortError, Message

from aiguard import storage
from aiguard.claude.proxy import (
    ClaudeProxy,
    _ai_guard_ui_url,
    _blocked_tool_response,
    _fetch_session_keys,
    _parse_anthropic_message,
    _parse_request_body,
    _parse_sse_body,
    _privacy_mode,
)
from aiguard.constants import AIGuardConstants

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _proxy() -> ClaudeProxy:
    return ClaudeProxy(upstream="http://upstream.invalid", blocking=True)


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


# ── claude/proxy.py — ClaudeProxy public surface ──────────────────────────────


class TestClaudeProxyMatches:
    """``ClaudeProxy.matches()`` is the routing key for Claude proxy traffic
    and decides on User-Agent only; method/path filtering happens inside
    ``parse_request`` (it returns empty messages for non-Anthropic URLs).

    The proxy is per-agent — each handler also exposes ``agent()`` and
    ``upstream()`` so the generic ``Proxy`` knows what to call it and where
    to forward claimed traffic. There is no default upstream.
    """

    @staticmethod
    def _req(
        method: str,
        path: str,
        ua: str = "claude-cli/1.2.3",
        *,
        session_id: str = "",
        agent_id: str = "",
    ) -> Any:
        headers = {"User-Agent": ua, "Content-Type": "application/json"} if ua else {}
        if session_id:
            headers["X-Claude-Code-Session-Id"] = session_id
        if agent_id:
            headers["X-Claude-Code-Agent-Id"] = agent_id
        return make_mocked_request(method, path, headers=headers)

    def test_agent_name_is_claude(self) -> None:
        assert _proxy().agent() == "claude"

    def test_upstream_is_returned_from_constructor(self) -> None:
        proxy = ClaudeProxy(upstream="https://api.anthropic.com", blocking=True)
        assert proxy.upstream() == "https://api.anthropic.com"

    def test_matches_when_user_agent_contains_claude_cli(self) -> None:
        assert _proxy().matches(self._req("POST", "/v1/messages")) is True

    def test_matches_regardless_of_method(self) -> None:
        # Method/path filtering is parse_request's job, not matches().
        assert _proxy().matches(self._req("GET", "/v1/messages")) is True

    def test_matches_regardless_of_path(self) -> None:
        assert _proxy().matches(self._req("POST", "/v1/anything-else")) is True

    def test_does_not_match_foreign_ua(self) -> None:
        assert _proxy().matches(self._req("POST", "/v1/messages", ua="curl/8.0")) is False

    def test_does_not_match_empty_ua(self) -> None:
        assert _proxy().matches(self._req("POST", "/v1/messages", ua="")) is False

    def test_parse_request_returns_empty_for_non_messages_path(self) -> None:
        proxy = _proxy()
        sid, aid, msgs = proxy.parse_request(self._req("POST", "/v1/something_else"), b"{}")
        assert (sid, aid, msgs) == ("", "", [])

    def test_parse_request_returns_empty_for_non_post(self) -> None:
        proxy = _proxy()
        sid, aid, msgs = proxy.parse_request(self._req("GET", "/v1/messages"), b"{}")
        assert (sid, aid, msgs) == ("", "", [])

    def test_parse_request_tolerates_invalid_json(self) -> None:
        proxy = _proxy()
        sid, aid, msgs = proxy.parse_request(self._req("POST", "/v1/messages"), b"{not json")
        assert (sid, aid, msgs) == ("", "", [])

    def test_parse_request_pulls_session_and_agent_id_from_headers(self) -> None:
        proxy = _proxy()
        body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
        request = self._req(
            "POST",
            "/v1/messages",
            session_id="sess-abc",
            agent_id="a14cb3bf47e9a6e48",
        )
        sid, aid, msgs = proxy.parse_request(request, body)
        assert sid == "sess-abc"
        assert aid == "a14cb3bf47e9a6e48"
        assert msgs and msgs[0]["role"] == "user"

    def test_parse_request_returns_empty_agent_id_for_main_session(self) -> None:
        proxy = _proxy()
        body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
        request = self._req("POST", "/v1/messages", session_id="sess-abc")
        sid, aid, _ = proxy.parse_request(request, body)
        assert sid == "sess-abc"
        assert aid == ""


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
        # Highest-confidence tag is listed first in the breakdown.
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
        result = _blocked_tool_response(event, abort)

        context = result["hookSpecificOutput"]["additionalContext"]
        url_prefix = "https://app.datadoghq.com/security/ai-guard/investigate?query="
        assert f"- Investigate in Datadog: {url_prefix}" in context
        assert sid in context
        # Model is told to surface the link in its reply.
        assert "include it in the response" in context

    def test_post_tool_use_includes_ui_url_with_dd_site(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DD_SITE", "datad0g.com")

        abort = AIGuardAbortError(action="DENY", reason="leaked_secret", tags=["t"])
        event = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "sess-42"}
        result = _blocked_tool_response(event, abort)

        context = result["hookSpecificOutput"]["additionalContext"]
        url_prefix = "https://app.datad0g.com/security/ai-guard/investigate?query="
        assert f"- Investigate in Datadog: {url_prefix}" in context
        assert "sess-42" in context

    def test_omits_ui_url_when_session_id_missing(self) -> None:
        abort = AIGuardAbortError(action="DENY", reason="r", tags=["t"])
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash"}
        result = _blocked_tool_response(event, abort)

        context = result["hookSpecificOutput"]["additionalContext"]
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
        # us3 / us5 / ap1 already carry their subdomain — the UI is reached at
        # the bare site host. Adding ``app.`` would point at a non-existent host.
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


# ── claude/proxy.py — message parsers ─────────────────────────────────────────


class TestRequestParser:
    """``_parse_request_body`` flattens ``system`` + ``messages`` into Messages."""

    def test_string_system_creates_system_message(self) -> None:
        out = _parse_request_body({"system": "be nice", "messages": []})
        assert out == [{"role": "system", "content": "be nice"}]

    def test_list_system_creates_system_with_parts(self) -> None:
        out = _parse_request_body(
            {
                "system": [{"type": "text", "text": "hi"}, {"type": "text", "text": "hello"}],
                "messages": [],
            }
        )
        assert out[0]["role"] == "system"
        parts = out[0]["content"]
        assert isinstance(parts, list)
        assert [p["type"] for p in parts] == ["text", "text"]

    def test_no_system_no_messages_returns_empty(self) -> None:
        assert _parse_request_body({}) == []

    def test_emits_messages_in_order(self) -> None:
        out = _parse_request_body(
            {
                "messages": [
                    {"role": "user", "content": "a"},
                    {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
                    {"role": "user", "content": "c"},
                ]
            }
        )
        roles = [m["role"] for m in out]
        assert roles == ["user", "assistant", "user"]

    def test_full_fixture_round_trips(self) -> None:
        data = json.loads((FIXTURES / "anthropic_messages_request.json").read_text())
        out = _parse_request_body(data)
        roles = [m["role"] for m in out]
        # system, user, assistant, tool (from tool_result), user (with non-tool blocks)
        assert roles[0] == "system"
        assert "tool" in roles
        assert roles.count("user") >= 2


class TestAnthropicMessageParser:
    """``_parse_anthropic_message`` converts a single Anthropic message dict."""

    def test_assistant_text_only(self) -> None:
        out = _parse_anthropic_message(
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}
        )
        assert len(out) == 1
        msg = out[0]
        assert msg["role"] == "assistant"
        assert msg["content"][0]["type"] == "text"
        assert msg["content"][0]["text"] == "hi"
        assert "tool_calls" not in msg

    def test_assistant_thinking_excluded(self) -> None:
        out = _parse_anthropic_message(
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "secret"},
                    {"type": "text", "text": "visible"},
                ],
            }
        )
        msg = out[0]
        types = [p["type"] for p in msg["content"]]
        assert "thinking" not in types
        assert "text" in types

    def test_assistant_tool_use_becomes_tool_calls(self) -> None:
        out = _parse_anthropic_message(
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu1", "name": "Read", "input": {"path": "x"}}
                ],
            }
        )
        msg = out[0]
        assert msg["role"] == "assistant"
        assert "content" not in msg
        assert msg["tool_calls"][0]["id"] == "tu1"
        assert msg["tool_calls"][0]["function"]["name"] == "Read"
        assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"path": "x"}

    def test_assistant_text_and_tool_use_both_present(self) -> None:
        out = _parse_anthropic_message(
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "ok"},
                    {"type": "tool_use", "id": "t", "name": "N", "input": {}},
                ],
            }
        )
        msg = out[0]
        assert msg["content"][0]["type"] == "text"
        assert msg["tool_calls"][0]["id"] == "t"

    def test_user_tool_result_becomes_tool_role_message(self) -> None:
        out = _parse_anthropic_message(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu7", "content": "OK"},
                    {"type": "text", "text": "thanks"},
                ],
            }
        )
        roles = [m["role"] for m in out]
        assert roles == ["tool", "user"]
        tool_msg = out[0]
        assert tool_msg["tool_call_id"] == "tu7"
        assert tool_msg["content"] == "OK"

    def test_user_string_content_passes_through(self) -> None:
        out = _parse_anthropic_message({"role": "user", "content": "hey"})
        assert out == [{"role": "user", "content": "hey"}]

    def test_user_image_block_preserved_as_serialized_part(self) -> None:
        out = _parse_anthropic_message(
            {
                "role": "user",
                "content": [{"type": "image", "source": {"type": "base64", "data": "AAA"}}],
            }
        )
        msg = out[0]
        assert msg["role"] == "user"
        assert msg["content"][0]["type"] == "image"
        # Whole block serialized as JSON in the .text attribute.
        payload = json.loads(msg["content"][0]["text"])
        assert payload["type"] == "image"

    def test_message_without_role_returns_empty(self) -> None:
        assert _parse_anthropic_message({"content": "x"}) == []


class TestSessionKeysParser:
    """``_fetch_session_keys`` extracts ``(session_id, agent_id)`` from the
    Claude Code request headers. Subagent calls share the parent's session id
    but add an ``X-Claude-Code-Agent-Id`` header so the proxy can route their
    history to a per-agent storage slot.

    When the session header is absent the session id falls back to the
    JSON-encoded ``metadata.user_id`` in the request body. The subagent id has
    no body equivalent, so it stays empty in that case.
    """

    @staticmethod
    def _request(**headers: str) -> Any:
        return make_mocked_request("POST", "/v1/messages", headers=headers)

    @staticmethod
    def _meta(session_id: str) -> dict[str, Any]:
        return {"metadata": {"user_id": json.dumps({"session_id": session_id})}}

    def test_session_only_header(self) -> None:
        req = self._request(**{"X-Claude-Code-Session-Id": "sess-abc"})
        assert _fetch_session_keys(req, {}) == ("sess-abc", "")

    def test_subagent_call_returns_session_and_agent_id(self) -> None:
        req = self._request(
            **{
                "X-Claude-Code-Session-Id": "sess-abc",
                "X-Claude-Code-Agent-Id": "a14cb3bf47e9a6e48",
            }
        )
        assert _fetch_session_keys(req, {}) == ("sess-abc", "a14cb3bf47e9a6e48")

    def test_returns_empty_when_no_headers(self) -> None:
        req = self._request()
        assert _fetch_session_keys(req, {}) == ("", "")

    def test_returns_empty_when_only_agent_id_header_present(self) -> None:
        """An agent_id without a session_id can't be routed; surface as empty."""
        req = self._request(**{"X-Claude-Code-Agent-Id": "a7"})
        sid, _ = _fetch_session_keys(req, {})
        assert sid == ""

    def test_header_lookup_is_case_insensitive(self) -> None:
        """HTTP header names are case-insensitive — lowercase still works."""
        req = self._request(
            **{
                "x-claude-code-session-id": "sess-abc",
                "x-claude-code-agent-id": "a7",
            }
        )
        assert _fetch_session_keys(req, {}) == ("sess-abc", "a7")

    def test_session_falls_back_to_metadata_when_header_absent(self) -> None:
        """No session header: recover the session id from metadata.user_id."""
        req = self._request()
        assert _fetch_session_keys(req, self._meta("sess-meta")) == ("sess-meta", "")

    def test_header_takes_precedence_over_metadata(self) -> None:
        req = self._request(**{"X-Claude-Code-Session-Id": "sess-hdr"})
        assert _fetch_session_keys(req, self._meta("sess-meta")) == ("sess-hdr", "")

    def test_metadata_fallback_keeps_agent_id_from_header(self) -> None:
        """Subagent id has no body equivalent, but the header still applies."""
        req = self._request(**{"X-Claude-Code-Agent-Id": "a7"})
        assert _fetch_session_keys(req, self._meta("sess-meta")) == ("sess-meta", "a7")

    def test_malformed_metadata_yields_empty_session(self) -> None:
        req = self._request()
        assert _fetch_session_keys(req, {"metadata": {"user_id": "not-json"}}) == ("", "")


class TestSSEResponseParser:
    """``_parse_sse_body`` reconstructs an assistant message from SSE chunks."""

    def test_text_concatenated(self) -> None:
        body = (
            b'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
            b'"content_block":{"type":"text","text":""}}\n\n'
            b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"hel"}}\n\n'
            b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"lo"}}\n\n'
        )
        out = _parse_sse_body(body)
        assert len(out) == 1
        assert out[0]["role"] == "assistant"
        assert out[0]["content"][0]["text"] == "hello"

    def test_tool_use_input_json_concatenated(self) -> None:
        body = (
            b'data: {"type":"content_block_start","index":0,'
            b'"content_block":{"type":"tool_use","id":"tu1","name":"Read","input":{}}}\n\n'
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"input_json_delta","partial_json":"{\\"a\\":"}}\n\n'
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"input_json_delta","partial_json":"1}"}}\n\n'
        )
        out = _parse_sse_body(body)
        msg = out[0]
        assert "tool_calls" in msg
        assert msg["tool_calls"][0]["function"]["arguments"] == '{"a":1}'

    def test_thinking_blocks_dropped(self) -> None:
        body = (
            b'data: {"type":"content_block_start","index":0,'
            b'"content_block":{"type":"thinking"}}\n\n'
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"thinking_delta","thinking":"oh"}}\n\n'
        )
        assert _parse_sse_body(body) == []

    def test_done_marker_ignored(self) -> None:
        body = (
            b'data: {"type":"content_block_start","index":0,'
            b'"content_block":{"type":"text","text":""}}\n\n'
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"x"}}\n\n'
            b"data: [DONE]\n\n"
        )
        out = _parse_sse_body(body)
        assert out[0]["content"][0]["text"] == "x"

    def test_invalid_json_chunks_tolerated(self) -> None:
        body = (
            b"data: not json\n\n"
            b'data: {"type":"content_block_start","index":0,'
            b'"content_block":{"type":"text","text":""}}\n\n'
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"y"}}\n\n'
        )
        out = _parse_sse_body(body)
        assert out[0]["content"][0]["text"] == "y"

    def test_unknown_block_type_preserved_as_json(self) -> None:
        body = (
            b'data: {"type":"content_block_start","index":0,'
            b'"content_block":{"type":"image","data":"AAA"}}\n\n'
        )
        out = _parse_sse_body(body)
        msg = out[0]
        parts = msg["content"]
        assert parts[0]["type"] == "image"
        payload = json.loads(parts[0]["text"])
        assert payload["type"] == "image"

    def test_anthropic_fixture_round_trip(self) -> None:
        body = (FIXTURES / "anthropic_sse_stream.txt").read_bytes()
        out = _parse_sse_body(body)
        msg = out[0]
        assert msg["content"][0]["text"] == "Hello world"
        assert msg["tool_calls"][0]["function"]["name"] == "Read"
        assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"path": "README.md"}

    def test_empty_body_returns_empty_list(self) -> None:
        assert _parse_sse_body(b"") == []


# ── claude/proxy.py — handle_hook dispatch ────────────────────────────────────


class TestHandleHookSpans:
    """Session/Subagent hooks emit a span tagged with the event metadata."""

    async def test_session_start(self, tracer_recorder, tmp_home: Path) -> None:
        out = await _proxy().handle_hook(
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

    async def test_session_end(self, tracer_recorder, tmp_home: Path) -> None:
        await _proxy().handle_hook(
            "SessionEnd", json.dumps({"session_id": "sX", "reason": "logout"}).encode()
        )
        assert tracer_recorder.spans[0].name == AIGuardConstants.SESSION_END
        assert tracer_recorder.spans[0].tags[AIGuardConstants.SESSION_ID_TAG] == "sX"

    async def test_session_end_clears_conversation_history(
        self, tracer_recorder, tmp_home: Path
    ) -> None:
        storage.save_messages("claude", "s-end", [Message(role="user", content="hi")])
        # Subagents ran under the same session — their slots must be cleared too.
        storage.save_messages(
            "claude", "s-end", [Message(role="user", content="sub")], agent_id="a7"
        )
        assert storage.load_messages("claude", "s-end")
        assert storage.load_messages("claude", "s-end", "a7")

        await _proxy().handle_hook("SessionEnd", json.dumps({"session_id": "s-end"}).encode())

        assert storage.load_messages("claude", "s-end") == []
        assert storage.load_messages("claude", "s-end", "a7") == []

    async def test_session_end_without_session_id_is_a_no_op(
        self, tracer_recorder, tmp_home: Path
    ) -> None:
        # Pre-populate something on the agent's storage that should NOT be touched.
        storage.save_messages("claude", "other", [Message(role="user", content="keep")])
        await _proxy().handle_hook("SessionEnd", b"{}")
        assert storage.load_messages("claude", "other") == [Message(role="user", content="keep")]

    async def test_subagent_start(self, tracer_recorder, tmp_home: Path) -> None:
        await _proxy().handle_hook(
            "SubagentStart",
            json.dumps({"session_id": "s1", "agent_id": "a7", "agent_type": "general"}).encode(),
        )
        span = tracer_recorder.spans[0]
        assert span.name == AIGuardConstants.SUBAGENT_START
        assert span.tags[AIGuardConstants.SESSION_ID_TAG] == "s1"
        assert span.tags[AIGuardConstants.SUBAGENT_ID_TAG] == "a7"
        assert span.tags[AIGuardConstants.SUBAGENT_TYPE_TAG] == "general"

    async def test_subagent_stop(self, tracer_recorder, tmp_home: Path) -> None:
        await _proxy().handle_hook(
            "SubagentStop",
            json.dumps({"session_id": "s1", "agent_id": "a8", "agent_type": "explorer"}).encode(),
        )
        span = tracer_recorder.spans[0]
        assert span.name == AIGuardConstants.SUBAGENT_STOP
        assert span.tags[AIGuardConstants.SUBAGENT_ID_TAG] == "a8"
        assert span.tags[AIGuardConstants.SUBAGENT_TYPE_TAG] == "explorer"


class TestHandleHookToolUse:
    """Pre/Post tool hooks evaluate via AI Guard and shape the response."""

    @staticmethod
    def _seed_pending_tool_call(
        session_id: str,
        *,
        name: str = "Bash",
        args: str = '{"command": "ls"}',
    ) -> None:
        """Seed storage with the assistant's tool-call message — what the
        proxy would have already persisted from the LLM response by the time
        the PreToolUse hook fires."""
        storage.save_messages(
            "claude",
            session_id,
            [
                Message(role="user", content="please run something"),
                Message(
                    role="assistant",
                    tool_calls=[{"id": "tu1", "function": {"name": name, "arguments": args}}],
                ),
            ],
        )

    async def test_pre_tool_use_evaluates_pending_tool_call(
        self, tracer_recorder, tmp_home: Path, fake_ai_guard
    ) -> None:
        self._seed_pending_tool_call("s-pre")
        out = await _proxy().handle_hook(
            "PreToolUse",
            json.dumps(
                {
                    "session_id": "s-pre",
                    "tool_use_id": "tu1",
                    "tool_name": "Bash",
                    "tool_input": {"command": "ls"},
                }
            ).encode(),
        )

        assert out == b""
        assert tracer_recorder.spans[0].name == AIGuardConstants.PRE_TOOL
        assert len(fake_ai_guard.calls) == 1
        messages, _ = fake_ai_guard.calls[0]
        last = messages[-1]
        assert last["role"] == "assistant"
        assert last["tool_calls"][0]["function"]["name"] == "Bash"

    async def test_pre_tool_use_injects_skill_markdown_as_tool_message(
        self, tracer_recorder, tmp_home: Path, fake_ai_guard
    ) -> None:
        """When the pending tool call is a Skill invocation, the SKILL.md body
        is appended as a tool-role message so AI Guard evaluates it."""
        skill_dir = tmp_home / ".claude" / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# my-skill body")

        storage.save_messages(
            "claude",
            "s-skill",
            [
                Message(role="user", content="run a skill"),
                Message(role="assistant", content="ok, loading"),
            ],
        )

        out = await _proxy().handle_hook(
            "PreToolUse",
            json.dumps(
                {
                    "session_id": "s-skill",
                    "cwd": str(tmp_home),
                    "tool_use_id": "tu1",
                    "tool_name": "Skill",
                    "tool_input": {"skill": "my-skill"},
                }
            ).encode(),
        )

        assert out == b""
        messages, _ = fake_ai_guard.calls[0]
        last = messages[-1]
        assert last["role"] == "tool"
        assert last["tool_call_id"] == "tu1"
        assert "my-skill body" in last["content"]

    async def test_pre_tool_use_skill_lookup_honours_claude_config_dir(
        self,
        tracer_recorder,
        tmp_home: Path,
        fake_ai_guard,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """User-level skill lookup follows ``$CLAUDE_CONFIG_DIR`` when set."""
        override = tmp_home / "work-claude"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(override))
        skill_dir = override / "skills" / "scoped-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# scoped-skill body")

        # Sibling skill at the default location must NOT be picked up — the
        # override is the only place we should resolve user-level skills from.
        decoy = tmp_home / ".claude" / "skills" / "scoped-skill"
        decoy.mkdir(parents=True)
        (decoy / "SKILL.md").write_text("# decoy body")

        storage.save_messages(
            "claude",
            "s-skill-override",
            [
                Message(role="user", content="run a skill"),
                Message(role="assistant", content="ok, loading"),
            ],
        )

        out = await _proxy().handle_hook(
            "PreToolUse",
            json.dumps(
                {
                    "session_id": "s-skill-override",
                    # cwd outside tmp_home so the project-walk branch finds nothing.
                    "cwd": "/",
                    "tool_use_id": "tu1",
                    "tool_name": "Skill",
                    "tool_input": {"skill": "scoped-skill"},
                }
            ).encode(),
        )

        assert out == b""
        messages, _ = fake_ai_guard.calls[0]
        last = messages[-1]
        assert last["role"] == "tool"
        assert "scoped-skill body" in last["content"]
        assert "decoy body" not in last["content"]

    async def test_pre_tool_use_returns_deny_payload_when_ai_guard_aborts(
        self, tracer_recorder, tmp_home: Path, fake_ai_guard
    ) -> None:
        self._seed_pending_tool_call("s-deny", name="Bash", args='{"command": "rm -rf /"}')
        fake_ai_guard.queue_abort(
            AIGuardAbortError(action="DENY", reason="prompt_injection", tags=["t"])
        )

        out = await _proxy().handle_hook(
            "PreToolUse",
            json.dumps(
                {
                    "session_id": "s-deny",
                    "hook_event_name": "PreToolUse",
                    "tool_use_id": "tu1",
                    "tool_name": "Bash",
                    "tool_input": {"command": "rm -rf /"},
                }
            ).encode(),
        )

        body = json.loads(out)
        hook_out = body["hookSpecificOutput"]
        assert hook_out["hookEventName"] == "PreToolUse"
        assert hook_out["permissionDecision"] == "deny"
        assert "Datadog AI Guard" in hook_out["permissionDecisionReason"]
        assert "prompt_injection" in hook_out["additionalContext"]

    async def test_post_tool_use_appends_tool_message_and_evaluates(
        self, tracer_recorder, tmp_home: Path, fake_ai_guard
    ) -> None:
        out = await _proxy().handle_hook(
            "PostToolUse",
            json.dumps(
                {
                    "session_id": "s-post",
                    "tool_use_id": "tu1",
                    "tool_name": "Read",
                    "tool_response": "file contents",
                }
            ).encode(),
        )

        assert out == b""
        assert tracer_recorder.spans[0].name == AIGuardConstants.POST_TOOL
        messages, _ = fake_ai_guard.calls[0]
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["tool_call_id"] == "tu1"
        assert messages[-1]["content"] == "file contents"

    async def test_post_tool_use_returns_block_payload_when_ai_guard_aborts(
        self, tracer_recorder, tmp_home: Path, fake_ai_guard
    ) -> None:
        fake_ai_guard.queue_abort(
            AIGuardAbortError(action="DENY", reason="leaked_secret", tags=["t"])
        )

        out = await _proxy().handle_hook(
            "PostToolUse",
            json.dumps(
                {
                    "session_id": "s-post",
                    "hook_event_name": "PostToolUse",
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

    async def test_post_tool_use_failure_appends_tool_error_message(
        self, tracer_recorder, tmp_home: Path, fake_ai_guard
    ) -> None:
        out = await _proxy().handle_hook(
            "PostToolUseFailure",
            json.dumps(
                {
                    "session_id": "s-fail",
                    "tool_use_id": "tu1",
                    "error": "permission denied",
                }
            ).encode(),
        )

        assert out == b""
        messages, _ = fake_ai_guard.calls[0]
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["content"] == "permission denied"

    async def test_pre_tool_use_loads_subagent_slot_not_main(
        self, tracer_recorder, tmp_home: Path, fake_ai_guard
    ) -> None:
        """A PreToolUse fired by a subagent must evaluate against the
        subagent's own conversation, never the parent session's history."""
        # Parent session: holds an unrelated conversation under the same sid.
        storage.save_messages(
            "claude",
            "s-mix",
            [Message(role="user", content="parent: please draft an email")],
        )
        # Subagent slot: pending tool call.
        storage.save_messages(
            "claude",
            "s-mix",
            [
                Message(role="user", content="sub: read README"),
                Message(
                    role="assistant",
                    tool_calls=[
                        {
                            "id": "tu1",
                            "function": {"name": "Read", "arguments": '{"path":"README.md"}'},
                        }
                    ],
                ),
            ],
            agent_id="a7",
        )

        await _proxy().handle_hook(
            "PreToolUse",
            json.dumps(
                {
                    "session_id": "s-mix",
                    "agent_id": "a7",
                    "tool_use_id": "tu1",
                    "tool_name": "Read",
                    "tool_input": {"path": "README.md"},
                }
            ).encode(),
        )

        messages, _ = fake_ai_guard.calls[0]
        # The user message that AI Guard saw is the subagent's, not the parent's.
        user_msgs = [m for m in messages if m.get("role") == "user"]
        assert user_msgs[0]["content"] == "sub: read README"
        assert all("parent:" not in str(m.get("content", "")) for m in messages)

    async def test_post_tool_use_persists_into_subagent_slot(
        self, tracer_recorder, tmp_home: Path, fake_ai_guard
    ) -> None:
        """PostToolUse for a subagent must read the subagent slot — proves the
        agent_id routes through every tool-hook handler, not just PreToolUse."""
        storage.save_messages(
            "claude",
            "s-post",
            [Message(role="user", content="parent context")],
        )
        storage.save_messages(
            "claude",
            "s-post",
            [Message(role="user", content="sub asked: cat the file")],
            agent_id="a9",
        )

        await _proxy().handle_hook(
            "PostToolUse",
            json.dumps(
                {
                    "session_id": "s-post",
                    "agent_id": "a9",
                    "tool_use_id": "tu2",
                    "tool_name": "Read",
                    "tool_response": "subagent file contents",
                }
            ).encode(),
        )

        messages, _ = fake_ai_guard.calls[0]
        user_msgs = [m for m in messages if m.get("role") == "user"]
        assert user_msgs[0]["content"] == "sub asked: cat the file"
        # Tool result appended onto the subagent's history, not the parent's.
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["content"] == "subagent file contents"


class TestHandleHookTags:
    """Span tagging picks up the ``user@host`` id from ``utils.fetch_endpoint_id``."""

    async def test_tags_user_id_from_fetch_endpoint_id(
        self, tracer_recorder, fake_endpoint_id: str, tmp_home: Path
    ) -> None:
        await _proxy().handle_hook("SessionStart", json.dumps({"session_id": "s1"}).encode())
        assert tracer_recorder.spans[0].tags[AIGuardConstants.USER_ID_TAG] == fake_endpoint_id


class TestHandleHookDispatch:
    """Payload tolerance + camelCase → snake_case method dispatch."""

    async def test_tolerates_invalid_json(self, tracer_recorder, tmp_home: Path) -> None:
        out = await _proxy().handle_hook("SessionStart", b"{not json")
        assert out == b""
        span = tracer_recorder.spans[0]
        assert span.name == AIGuardConstants.SESSION_START
        assert span.tags[AIGuardConstants.SESSION_ID_TAG] == ""

    async def test_tolerates_empty_payload(self, tracer_recorder, tmp_home: Path) -> None:
        out = await _proxy().handle_hook("SessionStart", b"")
        assert out == b""
        assert tracer_recorder.spans[0].name == AIGuardConstants.SESSION_START

    async def test_unknown_hook_emits_warning_no_span(
        self, tracer_recorder, tmp_home: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="ai_guard"):
            out = await _proxy().handle_hook("NotAHook", b"{}")
        assert out == b""
        assert tracer_recorder.spans == []
        assert any("unhandled hook" in rec.message for rec in caplog.records)

    async def test_serializes_dict_returned_by_method(
        self, tracer_recorder, tmp_home: Path
    ) -> None:
        """A handler method that returns a dict has its result JSON-encoded."""

        class _Echo(ClaudeProxy):
            async def _echo(self, event: dict[str, Any]) -> dict[str, Any]:
                return {"received": event}

        out = await _Echo(upstream="http://upstream.invalid", blocking=True).handle_hook(
            "Echo", json.dumps({"a": 1}).encode()
        )
        assert json.loads(out) == {"received": {"a": 1}}

    async def test_translates_camel_case_to_snake_case(self, tmp_home: Path) -> None:
        """``MyCustomHook`` is dispatched to ``_my_custom_hook``."""
        captured: dict = {}

        class _Sub(ClaudeProxy):
            async def _my_custom_hook(self, event: dict[str, Any]) -> None:
                captured["seen"] = event
                return None

        await _Sub(upstream="http://upstream.invalid", blocking=True).handle_hook(
            "MyCustomHook", json.dumps({"x": 1}).encode()
        )
        assert captured["seen"] == {"x": 1}
