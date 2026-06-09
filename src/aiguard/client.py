# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Coding-agent AI Guard client.

Thin layer over the released ``ddtrace.appsec.ai_guard.AIGuardClient``. The
released client has no seam for the two coding-agent features this project needs,
so instead of reimplementing ``evaluate`` we hook the ``ai_guard`` span via a
:class:`~ddtrace._trace.processor.SpanProcessor`:

* ``on_span_start`` applies ``Options.tags`` to the span (the released client
  ignores them) so spans stay queryable by session/user/model.
* ``on_span_finish`` redacts the meta-struct messages for ``CODING_AGENT``
  privacy mode: every message is kept but its content is replaced with
  ``[redacted]``. Redaction is unconditional (it does not depend on the AI
  Guard decision) so privacy mode can never surface full message content.

Both callbacks run synchronously inside the inherited ``evaluate`` — a per-call
context var carries the tags and original messages to them, while the processor
resolves the privacy mode once in its constructor. We also override
``_execute_request`` because ddtrace's HTTP helper has no proxy support.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import urllib.error
import urllib.request
from copy import deepcopy
from typing import Any, Optional, TypedDict

from ddtrace import config
from ddtrace._trace.processor import SpanProcessor
from ddtrace._trace.span import Span
from ddtrace.appsec._constants import AI_GUARD
from ddtrace.appsec.ai_guard import (
    AIGuardAbortError,
    AIGuardClient,
    AIGuardClientError,
    ContentPart,
    Evaluation,
    Function,
    ImageURL,
    Message,
    ToolCall,
)
from ddtrace.internal.settings.asm import ai_guard_config

from aiguard.constants import AIGuardConstants

__all__ = [
    "new_ai_guard_client",
    "AIGuardClient",
    "AIGuardClientError",
    "AIGuardAbortError",
    "ContentPart",
    "Evaluation",
    "Function",
    "ImageURL",
    "Message",
    "Options",
    "ToolCall",
]

logger = logging.getLogger("ai_guard")

REDACTED_PLACEHOLDER = "[redacted]"

_processor_registered = False

# Set around ``super().evaluate()`` so the span processor — which runs
# synchronously on span start/finish, within that same evaluate call — can read
# the per-evaluation tags and the original messages.
_eval_ctx: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "ai_guard_eval_ctx", default=None
)


class Options(TypedDict, total=False):
    """Evaluation options. Extends the released ``Options`` with ``tags``.

    Attributes:
        block: Whether non-ALLOW decisions raise ``AIGuardAbortError`` (defaults
            to the AI Guard response's ``is_blocking_enabled`` when omitted).
        tags: Extra tags to set on the ``ai_guard`` evaluation span.
    """

    block: bool
    tags: Optional[dict[str, Any]]


def _ensure_processor_registered() -> None:
    """Register the span processor exactly once, lazily at first client use."""
    global _processor_registered
    if not _processor_registered:
        _AIGuardSpanProcessor().register()
        _processor_registered = True


def _redact_json(value: Any) -> Any:
    """Redact a parsed JSON value, keeping only its top-level shape."""
    if isinstance(value, dict):
        return {key: REDACTED_PLACEHOLDER for key in value}
    if isinstance(value, list):
        return [f"[redacted {len(value)} entries]"]
    return REDACTED_PLACEHOLDER


def _redact_text(value: str) -> str:
    """Redact ``value``, descending into JSON structure when it is one."""
    stripped = value.strip()
    if stripped[:1] in ("{", "["):
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            return json.dumps(_redact_json(parsed), ensure_ascii=False)
    return REDACTED_PLACEHOLDER


def _redact_message_content(message: Message) -> Message:
    # deepcopy so the original message cannot be mutated before serialization
    message = deepcopy(message)
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = _redact_text(content)
    elif isinstance(content, list):
        # Multipart content: redact every part. Text parts get the placeholder;
        # image_url parts carry a hosted URL or a base64 ``data:`` payload that
        # would otherwise leak, so redact the url too.
        for part in content:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                part["text"] = _redact_text(part["text"])
            image_url = part.get("image_url")
            if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
                image_url["url"] = REDACTED_PLACEHOLDER
    # Tool calls carry their arguments as a (usually JSON) string; redact them
    # too so large/sensitive call payloads aren't surfaced.
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                function["arguments"] = _redact_text(function["arguments"])
    return message


def _redact_coding_agent_messages(messages: list[Message]) -> list[Message]:
    """Redact ``messages`` for ``CODING_AGENT`` privacy mode.

    Every message is kept (order and shape preserved) but its content is
    redacted to ``[redacted]`` so large amounts of (potentially sensitive)
    context aren't surfaced.
    """
    if not messages:
        return messages

    return [_redact_message_content(message) for message in messages]


class _Response:
    """Minimal HTTP response shim matching what the inherited ``evaluate`` reads."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def get_json(self) -> Optional[dict]:
        if not self._body:
            return None
        try:
            return json.loads(self._body)
        except (json.JSONDecodeError, ValueError):
            return None


class _AIGuardSpanProcessor(SpanProcessor):
    """Applies the coding-agent customizations to the ``ai_guard`` span.

    Registered once, globally; both callbacks return immediately for any span
    that isn't an ``ai_guard`` evaluation span.
    """

    def __init__(self) -> None:
        super().__init__()
        value = (os.environ.get(AIGuardConstants.PRIVACY_MODE_ENV) or "").strip().upper()
        self._privacy_mode = (
            AIGuardConstants.PRIVACY_MODE_DEFAULT
            if value == AIGuardConstants.PRIVACY_MODE_DEFAULT
            else AIGuardConstants.PRIVACY_MODE_CODING_AGENT
        )

    def on_span_start(self, span: Span) -> None:
        if span.name != AI_GUARD.RESOURCE_TYPE:
            return
        ctx = _eval_ctx.get()
        if not ctx:
            return
        tags = ctx.get("tags") or {}
        for key, value in tags.items():
            span.set_tag(key, value)

    def on_span_finish(self, span: Span) -> None:
        if span.name != AI_GUARD.RESOURCE_TYPE:
            return
        if self._privacy_mode != AIGuardConstants.PRIVACY_MODE_CODING_AGENT:
            logger.debug(
                "ai_guard span finish: privacy mode %s, leaving messages untouched",
                self._privacy_mode,
            )
            return
        ctx = _eval_ctx.get()
        if not ctx:
            return
        struct = span._get_struct_tag(AI_GUARD.STRUCT)
        if not struct:
            return
        redacted = _redact_coding_agent_messages(ctx["messages"])
        new_struct = dict(struct)
        new_struct["messages"] = redacted
        span._set_struct_tag(AI_GUARD.STRUCT, new_struct)
        logger.debug("ai_guard span finish: CODING_AGENT privacy redaction")

    def shutdown(self, timeout: Optional[float]) -> None:
        pass


class CodingAgentAIGuardClient(AIGuardClient):
    """Released AI Guard client + coding-agent tagging, privacy mode, and proxy.

    Evaluation logic is inherited; the coding-agent behaviour is applied by
    :class:`_AIGuardSpanProcessor` around the span ``super().evaluate()`` opens.
    """

    def __init__(
        self, endpoint: str, api_key: str, app_key: str, meta: Optional[dict[str, str]] = None
    ):
        super().__init__(endpoint, api_key, app_key)
        if meta:
            self._meta.update(meta)
        _ensure_processor_registered()

    def evaluate(self, messages: list[Message], options: Optional[Options] = None) -> Evaluation:
        # Publish the per-evaluation context for the span processor, then let the
        # inherited evaluate() run. evaluate() is synchronous, so the processor's
        # start/finish callbacks fire before this context var is reset.
        token = _eval_ctx.set(
            {
                "tags": (options or {}).get("tags") or {},
                "messages": messages,
            }
        )
        try:
            return super().evaluate(messages, options)
        finally:
            _eval_ctx.reset(token)

    def _execute_request(self, url: str, payload: Any) -> _Response:
        # Replaces ddtrace's get_connection (no proxy support) with urllib, whose
        # default ProxyHandler honours HTTP_PROXY/HTTPS_PROXY/NO_PROXY (and proxy
        # credentials). https-over-proxy uses CONNECT tunnelling automatically.
        body = json.dumps(payload, ensure_ascii=True, skipkeys=True, default=str).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=self._headers, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler())
        try:
            with opener.open(request, timeout=self._timeout) as response:
                return _Response(response.status, response.read())
        except urllib.error.HTTPError as exc:
            # Non-2xx: hand the status/body back so evaluate() raises a normal
            # AIGuardClientError instead of an opaque transport failure.
            try:
                body = exc.read()
            except Exception:
                body = b""
            return _Response(exc.code, body)


def new_ai_guard_client(
    endpoint: Optional[str] = None,
    meta: Optional[dict[str, str]] = None,
) -> CodingAgentAIGuardClient:
    api_key = config._dd_api_key
    app_key = config._dd_app_key
    if not api_key or not app_key:
        raise ValueError("Authentication credentials required: provide DD_API_KEY and DD_APP_KEY")

    if not endpoint:
        endpoint = ai_guard_config._ai_guard_endpoint
    if not endpoint:
        site = f"app.{config._dd_site}" if config._dd_site.count(".") == 1 else config._dd_site
        endpoint = f"https://{site}/api/v2/ai-guard"

    return CodingAgentAIGuardClient(endpoint, api_key, app_key, meta)
