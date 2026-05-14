"""Shared pytest fixtures for the ai-guard test suite."""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp
import aiohttp.web
import pytest
from aiohttp.test_utils import TestServer
from ddtrace.appsec.ai_guard import AIGuardAbortError, Message

from aiguard.claude.proxy import ClaudeProxy
from aiguard.proxy.server import Proxy

logger = logging.getLogger(__name__)

FIXTURE_DIR = Path(__file__).parent / "fixtures"


# ── Filesystem isolation ──────────────────────────────────────────────────────


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect Path.home() and HOME/USERPROFILE/DD_AI_GUARD_HOME to a tmp dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("DD_AI_GUARD_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


@pytest.fixture
def claude_user_json(tmp_home: Path) -> str:
    """Drop a sample ~/.claude.json into tmp_home; return the email."""
    src = FIXTURE_DIR / "claude_dot_json.json"
    shutil.copyfile(src, tmp_home / ".claude.json")
    data = json.loads(src.read_text())
    return data["oauthAccount"]["emailAddress"]


@pytest.fixture
def storage_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the storage module at an isolated DD_AI_GUARD_HOME for this test."""
    root = tmp_path / "ai_guard_home"
    monkeypatch.setenv("DD_AI_GUARD_HOME", str(root))
    return root


# ── Datadog tracer recorder ───────────────────────────────────────────────────


class _RecordedSpan:
    def __init__(
        self,
        name: str,
        resource: str | None,
        on_exit: Callable[["_RecordedSpan"], None] | None = None,
    ) -> None:
        self.name = name
        self.resource = resource
        self.tags: dict[str, object] = {}
        self._on_exit = on_exit

    def set_tag(self, key: str, value: object) -> None:
        self.tags[key] = value

    def __enter__(self) -> "_RecordedSpan":
        return self

    def __exit__(self, *exc: object) -> None:
        if self._on_exit is not None:
            self._on_exit(self)


class TracerRecorder:
    """Stand-in for ``ddtrace.tracer`` that records spans into a list.

    Supports both the ``with tracer.trace(...)`` pattern and ``@tracer.wrap``
    via ``current_span()`` lookups during the wrapped call.
    """

    def __init__(self) -> None:
        self.spans: list[_RecordedSpan] = []
        self._stack: list[_RecordedSpan] = []

    def trace(self, name: str, resource: str | None = None, **_: Any) -> _RecordedSpan:
        span = _RecordedSpan(name, resource, on_exit=self._pop)
        self.spans.append(span)
        self._stack.append(span)
        return span

    def _pop(self, span: _RecordedSpan) -> None:
        if self._stack and self._stack[-1] is span:
            self._stack.pop()
        elif span in self._stack:
            self._stack.remove(span)

    def current_span(self) -> _RecordedSpan | None:
        return self._stack[-1] if self._stack else None

    def shutdown(self) -> None:
        return None


@pytest.fixture
def tracer_recorder(monkeypatch: pytest.MonkeyPatch) -> TracerRecorder:
    """Redirect ``ddtrace.tracer`` traffic into a ``TracerRecorder``.

    ``@tracer.wrap(...)`` decorators applied at class-definition time captured
    the ddtrace singleton in their closure and call ``self.trace(...)`` /
    ``self.current_span()`` at invocation time. We mutate those methods on the
    same singleton so the existing wrappers transparently hit the recorder.
    """
    from ddtrace import tracer as _real_tracer

    rec = TracerRecorder()
    monkeypatch.setattr(_real_tracer, "trace", rec.trace, raising=False)
    monkeypatch.setattr(_real_tracer, "current_span", rec.current_span, raising=False)
    monkeypatch.setattr(_real_tracer, "shutdown", rec.shutdown, raising=False)
    return rec


# ── AI Guard client stub ──────────────────────────────────────────────────────


class FakeAIGuardClient:
    """Stand-in for ``ddtrace.appsec.ai_guard.AIGuardClient``.

    Records every ``evaluate(messages, options)`` call. Tests that want a
    blocking outcome can ``queue_abort(AIGuardAbortError(...))`` ahead of time;
    otherwise ``evaluate`` is a silent no-op.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[list[Message], dict[str, object]]] = []
        self._aborts: list[AIGuardAbortError] = []

    def queue_abort(self, abort: AIGuardAbortError) -> None:
        self._aborts.append(abort)

    def evaluate(self, messages: list[Message], options: dict[str, object]) -> None:
        self.calls.append((list(messages), dict(options) if options else {}))
        if self._aborts:
            raise self._aborts.pop(0)


@pytest.fixture(autouse=True)
def fake_ai_guard(monkeypatch: pytest.MonkeyPatch) -> FakeAIGuardClient:
    """Replace ``new_ai_guard_client`` so ``ClaudeProxy()`` doesn't need real DD keys.

    Autouse: every test that imports/instantiates ``ClaudeProxy`` (directly or
    via the proxy harness) gets the fake. Tests that want to assert evaluation
    calls or queue an abort take ``fake_ai_guard`` as a parameter.
    """
    fake = FakeAIGuardClient()
    monkeypatch.setattr(
        "aiguard.claude.proxy.new_ai_guard_client",
        lambda meta=None: fake,
        raising=False,
    )
    return fake


# ── Mock Anthropic upstream ───────────────────────────────────────────────────


@pytest.fixture
def anthropic_request_body() -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / "anthropic_messages_request.json").read_text())


@pytest.fixture
def anthropic_response_body() -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / "anthropic_messages_response.json").read_text())


@pytest.fixture
def anthropic_sse_body() -> bytes:
    return (FIXTURE_DIR / "anthropic_sse_stream.txt").read_bytes()


AnthropicHandler = Callable[[aiohttp.web.Request], Awaitable[aiohttp.web.StreamResponse]]


@pytest.fixture
async def anthropic_server() -> Any:
    """Spin up an aiohttp TestServer with a configurable handler.

    Yields a factory: `factory(handler)` returns the running server's URL str.
    """
    servers: list[TestServer] = []

    async def factory(handler: AnthropicHandler) -> str:
        app = aiohttp.web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        return str(server.make_url("/")).rstrip("/")

    yield factory

    for server in servers:
        await server.close()


# ── Proxy harness ─────────────────────────────────────────────────────────────


@pytest.fixture
async def proxy_client(
    anthropic_server, storage_root: Path, fake_ai_guard: "FakeAIGuardClient"
) -> Any:
    """Yield a factory that builds a proxy in front of a configurable upstream.

    Usage:
        client = await proxy_client(handler)
        async with client.post("/v1/messages", ...) as resp: ...
    """
    servers: list[TestServer] = []

    async def factory(
        upstream_handler: AnthropicHandler, *, blocking: bool = True
    ) -> aiohttp.test_utils.TestClient:
        upstream_url = await anthropic_server(upstream_handler)
        proxy = Proxy(
            host="127.0.0.1",
            port=0,
            handlers=[ClaudeProxy(upstream_url, blocking)],
        )
        app = proxy.build_app()
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        client = aiohttp.test_utils.TestClient(server)
        await client.start_server()
        return client

    yield factory

    for s in servers:
        await s.close()


# ── Misc ──────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_otel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid having ambient OTEL_ vars leak into the CLI under test."""
    for key in list(os.environ):
        if key.startswith("OTEL_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _quiet_ddtrace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the live ddtrace tracer + telemetry so tests don't try to reach
    a real agent (would otherwise add ~20s of connection/shutdown timeouts)."""
    monkeypatch.setenv("DD_TRACE_ENABLED", "false")
    monkeypatch.setenv("DD_INSTRUMENTATION_TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("DD_REMOTE_CONFIGURATION_ENABLED", "false")
    try:
        from ddtrace.trace import tracer as _tracer

        monkeypatch.setattr(_tracer, "enabled", False, raising=False)
    except (ImportError, AttributeError):
        # ddtrace internals can shift; the env vars above keep tracing quiet
        # even if we can't toggle the live tracer instance.
        logger.debug("conftest: could not disable live ddtrace tracer", exc_info=True)
