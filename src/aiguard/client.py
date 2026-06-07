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
* ``on_span_finish`` reduces the meta-struct messages for ``CODING_AGENT``
  privacy mode once the action is known (first user + last message; the kept
  messages' content is stripped on ``ALLOW`` or when no action was recorded, and
  retained only for a real non-``ALLOW`` block).

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
from ddtrace.appsec.ai_guard._api_client import ALLOW
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

TRUNCATE_PREFIX_LENGTH = 16

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


def _truncate_string(value: str) -> str:
    if len(value) <= TRUNCATE_PREFIX_LENGTH:
        return value
    return (
        value[:TRUNCATE_PREFIX_LENGTH] + f" [truncated {len(value) - TRUNCATE_PREFIX_LENGTH} chars]"
    )


def _truncate_json(value: Any) -> Any:
    """Recursively truncate string leaves of a parsed JSON value.

    Object keys are left intact; only string values (anywhere in the tree) are
    truncated. Numbers, booleans and ``null`` pass through unchanged.
    """
    if isinstance(value, str):
        return _truncate_string(value)
    if isinstance(value, list):
        return [_truncate_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _truncate_json(val) for key, val in value.items()}
    return value


def _truncate_text(value: str) -> str:
    """Truncate ``value``, descending into JSON structure when it is one.

    Tool arguments/results are frequently JSON. When ``value`` is a JSON object
    or array we truncate its string leaves (keeping keys and shape) and
    re-serialize, so the structure stays readable; otherwise the raw string is
    truncated.
    """
    stripped = value.strip()
    if stripped[:1] in ("{", "["):
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            return json.dumps(_truncate_json(parsed), ensure_ascii=False)
    return _truncate_string(value)


def _truncate_message_content(message: Message) -> Message:
    # deepcopy so the original message cannot be mutated before serialization
    message = deepcopy(message)
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = _truncate_text(content)
    elif isinstance(content, list):
        # Multipart content: truncate the text of each part, leaving non-text
        # parts (e.g. image_url) untouched.
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                part["text"] = _truncate_text(part["text"])
    # Tool calls carry their arguments as a (usually JSON) string; truncate them
    # too so large/sensitive call payloads aren't surfaced on an ALLOW.
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                function["arguments"] = _truncate_text(function["arguments"])
    return message


def _truncate_coding_agent_messages(
    messages: list[Message], action: Optional[str]
) -> list[Message]:
    """Reduce ``messages`` for ``CODING_AGENT`` privacy mode.

    Keep only the first user message and the last message, dropping everything in
    between to avoid surfacing large amounts of (potentially sensitive) context.
    Full content is retained only for a real non-``ALLOW`` decision (a block), so
    investigators see what was flagged. On an ``ALLOW`` decision — or when no
    action was recorded (e.g. an AI Guard error before the decision) — the kept
    messages' content is stripped so privacy mode isn't defeated by the error path.
    """
    if not messages:
        return messages

    # Retain full content only when AI Guard returned an actual non-ALLOW action.
    truncate = not action or action == ALLOW
    last_message = messages[-1]
    first_user_message = next(
        (message for message in messages if message.get("role") == "user"), None
    )

    result_messages: list[Message] = []
    if first_user_message is not None:
        result_messages.append(
            _truncate_message_content(first_user_message) if truncate else first_user_message
        )

    # When the first user message is also the last message keep a single copy.
    if first_user_message is not last_message:
        result_messages.append(
            _truncate_message_content(last_message) if truncate else last_message
        )

    return result_messages


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
        # The action tag is set by the inherited evaluate() before the span
        # finishes; absent only on an error path, where content is truncated so an
        # AI Guard failure can't surface full messages.
        action = span.get_tag(AI_GUARD.ACTION_TAG)
        truncated = _truncate_coding_agent_messages(ctx["messages"], action)
        new_struct = dict(struct)
        new_struct["messages"] = truncated
        span._set_struct_tag(AI_GUARD.STRUCT, new_struct)
        logger.debug("ai_guard span finish: CODING_AGENT privacy truncation (action=%s)", action)

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
