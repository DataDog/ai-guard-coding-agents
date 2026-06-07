# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Unit tests for src/aiguard/client.py.

The coding-agent client subclasses the released ddtrace ``AIGuardClient`` and
applies its customizations through a span processor rather than reimplementing
``evaluate``. These tests drive a real ``CodingAgentAIGuardClient`` (with a
stubbed HTTP layer) against the real tracer so the processor's ``on_span_start``
(tag injection) and ``on_span_finish`` (privacy-mode message reduction) actually
fire, and assert on the resulting ``ai_guard`` span.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Optional

import pytest
from ddtrace._trace.processor import SpanProcessor
from ddtrace.trace import tracer

import aiguard.client as client_module
from aiguard.client import (
    AIGuardAbortError,
    CodingAgentAIGuardClient,
    Message,
    _AIGuardSpanProcessor,
    _truncate_coding_agent_messages,
)
from aiguard.constants import AIGuardConstants

# The session/user/model tags the proxy passes via ``Options.tags``.
_TAGS = {"ai_guard.usr.session_id": "S1", "ai_guard.coding_agent": "claude_code"}


class _FakeResponse:
    """Minimal stand-in for ddtrace's HTTP ``Response`` used by ``evaluate``."""

    def __init__(self, action: str, *, blocking: bool) -> None:
        self.status = 200
        self._action = action
        self._blocking = blocking

    def get_json(self) -> dict:
        return {
            "data": {
                "attributes": {
                    "action": self._action,
                    "reason": "r",
                    "tags": ["prompt_injection"],
                    "is_blocking_enabled": self._blocking,
                    "tag_probs": {"prompt_injection": 0.9},
                }
            }
        }


_LAST_CONTENT = "LAST tool output, long enough to truncate"


def _messages() -> list[Message]:
    return [
        Message(role="user", content="FIRST user message, long enough to truncate"),
        Message(role="assistant", content="MIDDLE assistant chatter"),
        Message(role="tool", tool_call_id="t1", content=_LAST_CONTENT),
    ]


def _evaluate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    privacy_mode: str,
    action: str,
    blocking: bool = True,
    messages: Optional[list[Message]] = None,
):
    """Run an evaluation and return ``(span, blocked)``.

    Stubs the network call, captures the ``ai_guard`` span the inherited
    ``evaluate`` opens, and lets the real processor mutate it.
    """
    # conftest disables the live tracer (autouse) so tests don't reach an agent;
    # re-enable it here so the span processor's start/finish callbacks fire.
    monkeypatch.setattr(tracer, "enabled", True)

    # The processor resolves its privacy mode from the env in its constructor and
    # registers once globally; reset that registration so a fresh processor picks
    # up this test's mode. monkeypatch restores both after the test.
    monkeypatch.setenv(AIGuardConstants.PRIVACY_MODE_ENV, privacy_mode)
    monkeypatch.setattr(client_module, "_processor_registered", False)
    monkeypatch.setattr(
        SpanProcessor,
        "__processors__",
        [p for p in SpanProcessor.__processors__ if not isinstance(p, _AIGuardSpanProcessor)],
    )

    client = CodingAgentAIGuardClient(
        "https://example.invalid/api/v2/ai-guard",
        "api-key",
        "app-key",
        meta={"coding_agent": "claude_code"},
    )
    monkeypatch.setattr(
        client, "_execute_request", lambda url, payload: _FakeResponse(action, blocking=blocking)
    )

    spans = []
    real_trace = tracer.trace

    def _recording_trace(*args, **kwargs):
        cm = real_trace(*args, **kwargs)
        spans.append(cm)
        return cm

    monkeypatch.setattr(tracer, "trace", _recording_trace)

    blocked = False
    try:
        client.evaluate(
            messages if messages is not None else _messages(),
            {"block": blocking, "tags": _TAGS},
        )
    except AIGuardAbortError:
        blocked = True

    ai_guard_spans = [s for s in spans if s.name == "ai_guard"]
    assert len(ai_guard_spans) == 1
    return ai_guard_spans[0], blocked


def _struct_messages(span) -> list[Message]:
    return (span._get_struct_tag("ai_guard") or {}).get("messages", [])


class TestTagInjection:
    """``Options.tags`` land on the ai_guard span itself (released client drops them)."""

    def test_tags_set_on_span_in_coding_agent_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        span, _ = _evaluate(monkeypatch, privacy_mode="CODING_AGENT", action="ALLOW")
        assert span.get_tag("ai_guard.usr.session_id") == "S1"
        assert span.get_tag("ai_guard.coding_agent") == "claude_code"

    def test_tags_set_on_span_in_default_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        span, _ = _evaluate(monkeypatch, privacy_mode="DEFAULT", action="ALLOW")
        assert span.get_tag("ai_guard.usr.session_id") == "S1"


class TestCodingAgentPrivacy:
    """CODING_AGENT keeps first-user + last, action-dependent content stripping."""

    def test_allow_keeps_first_user_and_last_and_truncates_last(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("DEBUG", logger="ai_guard"):
            span, blocked = _evaluate(monkeypatch, privacy_mode="CODING_AGENT", action="ALLOW")
        messages = _struct_messages(span)
        assert [m.get("role") for m in messages] == ["user", "tool"]
        assert "[truncated" in messages[-1]["content"]
        assert blocked is False
        # The processor logs that it actually ran the privacy truncation.
        assert any(
            "CODING_AGENT privacy truncation" in r.message and "action=ALLOW" in r.message
            for r in caplog.records
        )

    def test_block_keeps_last_message_content_intact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        span, blocked = _evaluate(monkeypatch, privacy_mode="CODING_AGENT", action="DENY")
        messages = _struct_messages(span)
        assert [m.get("role") for m in messages] == ["user", "tool"]
        assert messages[-1]["content"] == _LAST_CONTENT
        assert span.get_tag("ai_guard.blocked") == "true"
        assert blocked is True


class TestDefaultMode:
    """DEFAULT mode keeps the inherited behavior (no first-user/last reduction)."""

    def test_keeps_all_messages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        span, _ = _evaluate(monkeypatch, privacy_mode="DEFAULT", action="ALLOW")
        messages = _struct_messages(span)
        assert [m.get("role") for m in messages] == ["user", "assistant", "tool"]


class TestTruncationByAction:
    """Content is kept only for a real non-ALLOW action; ALLOW/absent truncate."""

    def test_deny_keeps_full_content(self) -> None:
        result = _truncate_coding_agent_messages(_messages(), "DENY")
        assert [m.get("role") for m in result] == ["user", "tool"]
        assert result[-1]["content"] == _LAST_CONTENT

    def test_allow_truncates_content(self) -> None:
        result = _truncate_coding_agent_messages(_messages(), "ALLOW")
        assert [m.get("role") for m in result] == ["user", "tool"]
        assert "[truncated" in result[-1]["content"]

    def test_missing_action_truncates_content(self) -> None:
        # An AI Guard error finishes the span with no action recorded; privacy
        # mode must still strip content rather than leak it.
        result = _truncate_coding_agent_messages(_messages(), None)
        assert [m.get("role") for m in result] == ["user", "tool"]
        assert "[truncated" in result[-1]["content"]


class TestPrivacyModeResolution:
    """The processor resolves ``DD_AI_GUARD_PRIVACY_MODE`` in its constructor."""

    def test_defaults_to_coding_agent_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(AIGuardConstants.PRIVACY_MODE_ENV, raising=False)
        assert _AIGuardSpanProcessor()._privacy_mode == AIGuardConstants.PRIVACY_MODE_CODING_AGENT

    def test_honours_default_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(AIGuardConstants.PRIVACY_MODE_ENV, "DEFAULT")
        assert _AIGuardSpanProcessor()._privacy_mode == AIGuardConstants.PRIVACY_MODE_DEFAULT

    def test_is_case_insensitive_and_trims(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(AIGuardConstants.PRIVACY_MODE_ENV, "  default ")
        assert _AIGuardSpanProcessor()._privacy_mode == AIGuardConstants.PRIVACY_MODE_DEFAULT

    def test_unknown_value_falls_back_to_coding_agent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(AIGuardConstants.PRIVACY_MODE_ENV, "bogus")
        assert _AIGuardSpanProcessor()._privacy_mode == AIGuardConstants.PRIVACY_MODE_CODING_AGENT


class _FakeHTTPResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class TestExecuteRequestProxy:
    """``_execute_request`` posts via urllib and honors *_PROXY env vars."""

    def _client(self) -> CodingAgentAIGuardClient:
        return CodingAgentAIGuardClient("https://ep.invalid/api/v2/ai-guard", "k", "k")

    def test_posts_json_with_headers_and_parses_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = {}

        def fake_open(self, request, timeout=None):  # noqa: ANN001
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["body"] = request.data
            captured["has_api_key"] = any(k.lower() == "dd-api-key" for k in request.headers)
            return _FakeHTTPResponse(200, b'{"ok": true}')

        monkeypatch.setattr(urllib.request.OpenerDirector, "open", fake_open)

        resp = self._client()._execute_request(
            "https://ep.invalid/api/v2/ai-guard/evaluate", {"a": 1}
        )

        assert resp.status == 200
        assert resp.get_json() == {"ok": True}
        assert captured["method"] == "POST"
        assert b'"a"' in captured["body"]
        assert captured["has_api_key"]

    def test_uses_proxy_handler_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
        seen = {}
        real_build_opener = urllib.request.build_opener

        def spy_build_opener(*handlers):
            seen["handlers"] = handlers
            return real_build_opener(*handlers)

        monkeypatch.setattr(urllib.request, "build_opener", spy_build_opener)
        monkeypatch.setattr(
            urllib.request.OpenerDirector,
            "open",
            lambda self, request, timeout=None: _FakeHTTPResponse(200, b"{}"),
        )

        self._client()._execute_request("https://ep.invalid/api/v2/ai-guard/evaluate", {})

        proxy_handlers = [h for h in seen["handlers"] if isinstance(h, urllib.request.ProxyHandler)]
        assert proxy_handlers, "request should be built with a ProxyHandler"
        assert proxy_handlers[0].proxies.get("https") == "http://proxy.invalid:3128"

    def test_non_2xx_is_surfaced_as_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raise_http_error(self, request, timeout=None):  # noqa: ANN001
            raise urllib.error.HTTPError(request.full_url, 503, "busy", hdrs=None, fp=None)

        monkeypatch.setattr(urllib.request.OpenerDirector, "open", raise_http_error)

        resp = self._client()._execute_request("https://ep.invalid/api/v2/ai-guard/evaluate", {})
        assert resp.status == 503
