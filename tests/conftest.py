# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Shared pytest fixtures for the ai-guard test suite."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pytest

from aiguard import utils
from aiguard.client import AIGuardAbortError, Message
from tests.transcripts import TranscriptWriter

logger = logging.getLogger(__name__)


# ── Filesystem isolation ──────────────────────────────────────────────────────


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect Path.home() and HOME/USERPROFILE to a tmp dir.

    Also clears DD_AI_GUARD_HOME, XDG_CONFIG_HOME/XDG_STATE_HOME, and
    CLAUDE_CONFIG_DIR so the path helpers in ``aiguard.paths`` resolve under
    the tmp home and don't bleed into anything the host has exported.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("DD_AI_GUARD_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


@pytest.fixture
def fake_endpoint_id(monkeypatch: pytest.MonkeyPatch) -> str:
    """Pin ``aiguard.utils.fetch_endpoint_id`` to a deterministic ``user@host`` value."""
    value = "test-user@test-host"
    monkeypatch.setattr(utils, "fetch_endpoint_id", lambda: value)
    return value


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
        on_exit=None,
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
    """Stand-in for ``aiguard.client.CodingAgentAIGuardClient``.

    Records every ``evaluate(messages, options)`` call. Tests that want a
    blocking outcome can ``queue_abort(AIGuardAbortError(...))`` ahead of time;
    otherwise ``evaluate`` is a silent no-op.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[list[Message], dict[str, object]]] = []
        self._aborts: list[AIGuardAbortError] = []

    def queue_abort(self, abort: AIGuardAbortError) -> None:
        self._aborts.append(abort)

    @property
    def last_messages(self) -> list[Message]:
        """Messages passed to the most recent ``evaluate`` call."""
        assert self.calls, "evaluate() was never called"
        return self.calls[-1][0]

    def evaluate(self, messages: list[Message], options: dict[str, object]) -> None:
        self.calls.append((list(messages), dict(options) if options else {}))
        if self._aborts:
            raise self._aborts.pop(0)


@pytest.fixture(autouse=True)
def fake_ai_guard(monkeypatch: pytest.MonkeyPatch) -> FakeAIGuardClient:
    """Replace ``new_ai_guard_client`` so ``ClaudeHandler()`` doesn't need real DD keys.

    Autouse: every test that instantiates ``ClaudeHandler`` (directly or via the
    ``hook`` command) gets the fake. Tests that want to assert evaluation calls
    or queue an abort take ``fake_ai_guard`` as a parameter.
    """
    fake = FakeAIGuardClient()
    monkeypatch.setattr(
        "aiguard.claude.handler.new_ai_guard_client",
        lambda mode=None, meta=None: fake,
        raising=False,
    )
    return fake


# ── Claude transcript builder ──────────────────────────────────────────────────


@pytest.fixture
def transcripts(tmp_path: Path) -> TranscriptWriter:
    """A :class:`TranscriptWriter` rooted at an isolated fake project directory."""
    return TranscriptWriter(tmp_path / "projects" / "test-project")


# ── Misc ──────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to "no OS keychain" so secret handling is deterministic.

    The host running the suite may or may not have a working keychain backend;
    forcing :func:`aiguard.keychain._keyring` to report none here means the
    file-fallback path (secrets in config.env) is exercised consistently, while
    still running the real public functions. Tests that want the keychain path
    take the :func:`fake_keychain` fixture, which re-patches this afterwards.
    """
    from aiguard import keychain

    monkeypatch.setattr(keychain, "_keyring", lambda: None)


class _FakeKeyring:
    """Minimal in-memory keyring backend (``set/get/delete_password``)."""

    def __init__(self, vault: dict[str, str]) -> None:
        self._vault = vault

    def set_password(self, service: str, key: str, value: str) -> None:
        self._vault[key] = value

    def get_password(self, service: str, key: str) -> str | None:
        return self._vault.get(key)

    def delete_password(self, service: str, key: str) -> None:
        self._vault.pop(key, None)


@pytest.fixture
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Back :mod:`aiguard.keychain` with an in-memory store, keyed by env-var name.

    Returns the backing dict so tests can assert what landed in the keychain
    (and seed it to simulate a prior install). The real ``store``/``load``/
    ``delete`` logic runs on top of the fake backend.
    """
    from aiguard import keychain

    vault: dict[str, str] = {}
    monkeypatch.setattr(keychain, "_keyring", lambda: _FakeKeyring(vault))
    return vault


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
