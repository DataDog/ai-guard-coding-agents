# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Unit tests for the ``ai-guard`` CLI.

Two surfaces:

  * :class:`TestMainCli` / :class:`TestSetupLogging` — the top-level
    ``ai-guard`` entry point and its log-file plumbing.
  * :class:`TestHookCli` — the ``ai-guard hook AGENT HOOK`` command, a thin
    HTTP shim to a running proxy. Errors are swallowed (logged via
    ``logger.exception`` and exit 0) so a failing hook never breaks the
    calling agent's command flow.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from aiguard import __version__
from aiguard.cli import _setup_logging, main
from aiguard.hooks.hooks import hook

# ── ai-guard (top-level) ──────────────────────────────────────────────────────


class TestMainCli:
    def test_version(self) -> None:
        result = CliRunner().invoke(main, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_help_lists_subcommands(self) -> None:
        result = CliRunner().invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "hook" in result.output
        assert "proxy" in result.output


class TestSetupLogging:
    def test_none_attaches_null_handler(self) -> None:
        logger = logging.getLogger("ai_guard")
        before = list(logger.handlers)
        try:
            _setup_logging(None)
            assert any(isinstance(h, logging.NullHandler) for h in logger.handlers)
        finally:
            logger.handlers = before

    def test_writes_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "x" / "ai.log"
        logger = logging.getLogger("ai_guard")
        before = list(logger.handlers)
        before_level = logger.level
        try:
            _setup_logging(str(log_file))
            logger.debug("hello world")
            for h in logger.handlers:
                h.flush()
            assert log_file.exists()
            assert "hello world" in log_file.read_text()
            assert logger.level == logging.DEBUG
        finally:
            for h in list(logger.handlers):
                if h not in before:
                    logger.removeHandler(h)
                    h.close()
            logger.setLevel(before_level)


# ── ai-guard hook AGENT HOOK ──────────────────────────────────────────────────


def _start_mock_proxy(state: dict[str, Any]) -> tuple[HTTPServer, threading.Thread]:
    """Start an in-process HTTP server that captures requests into ``state``."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            state["calls"].append(
                {
                    "path": self.path,
                    "body": body,
                    "content_type": self.headers.get("Content-Type", ""),
                }
            )

            outcome = state.get("outcome", {"status": 204, "body": b""})
            status = outcome.get("status", 200)
            reply = outcome.get("body", b"")
            content_type = outcome.get("content_type", "application/json")

            self.send_response(status)
            if reply:
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(reply)))
            else:
                self.send_header("Content-Length", "0")
            self.end_headers()
            if reply:
                self.wfile.write(reply)

        def log_message(self, fmt: str, *args: Any) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture
def mock_proxy() -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"calls": [], "outcome": {"status": 204, "body": b""}}
    server, thread = _start_mock_proxy(state)
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _invoke(args: list[str], stdin: str = "{}") -> Any:
    return CliRunner().invoke(hook, args, input=stdin)


class TestHookCli:
    """The ``hook`` command POSTs stdin to the proxy and writes the reply."""

    def test_posts_stdin_to_correct_path(self, mock_proxy: dict[str, Any]) -> None:
        payload = json.dumps({"session_id": "s1"})
        result = _invoke(
            ["claude", "session-start", "--proxy-url", mock_proxy["url"]],
            stdin=payload,
        )

        assert result.exit_code == 0, result.output
        assert len(mock_proxy["calls"]) == 1
        call = mock_proxy["calls"][0]
        assert call["path"] == "/hook/claude/session-start"
        assert call["body"] == payload.encode()
        assert call["content_type"] == "application/json"

    def test_writes_response_body_to_stdout(self, mock_proxy: dict[str, Any]) -> None:
        mock_proxy["outcome"] = {"status": 200, "body": b'{"ok": true}'}
        result = _invoke(["claude", "session-start", "--proxy-url", mock_proxy["url"]])
        assert result.exit_code == 0
        assert result.output.strip() == '{"ok": true}'

    def test_writes_nothing_on_204(self, mock_proxy: dict[str, Any]) -> None:
        mock_proxy["outcome"] = {"status": 204, "body": b""}
        result = _invoke(["claude", "session-end", "--proxy-url", mock_proxy["url"]])
        assert result.exit_code == 0
        assert result.output == ""

    def test_logs_4xx_but_does_not_fail(
        self, mock_proxy: dict[str, Any], caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_proxy["outcome"] = {
            "status": 404,
            "body": b"no handler for agent 'bogus'",
            "content_type": "text/plain",
        }
        with caplog.at_level(logging.ERROR, logger="ai_guard"):
            result = _invoke(["bogus", "session-start", "--proxy-url", mock_proxy["url"]])

        # CLI never breaks the host agent: exit 0, body absorbed, error logged.
        assert result.exit_code == 0
        assert result.output == ""
        assert any("failed to invoke hook" in rec.message for rec in caplog.records)

    def test_logs_5xx_but_does_not_fail(
        self, mock_proxy: dict[str, Any], caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_proxy["outcome"] = {"status": 500, "body": b"", "content_type": "text/plain"}
        with caplog.at_level(logging.ERROR, logger="ai_guard"):
            result = _invoke(["claude", "session-start", "--proxy-url", mock_proxy["url"]])
        assert result.exit_code == 0
        assert any("failed to invoke hook" in rec.message for rec in caplog.records)

    def test_proxy_unreachable_logs_but_does_not_fail(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Pick a port that is almost certainly closed.
        with caplog.at_level(logging.ERROR, logger="ai_guard"):
            result = _invoke(
                ["claude", "session-start", "--proxy-url", "http://127.0.0.1:1"],
            )
        assert result.exit_code == 0
        assert any("failed to invoke hook" in rec.message for rec in caplog.records)

    def test_uses_env_var_for_proxy_url(
        self, mock_proxy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DD_AI_GUARD_PROXY_URL", mock_proxy["url"])
        result = _invoke(["claude", "session-start"])
        assert result.exit_code == 0
        assert mock_proxy["calls"][0]["path"] == "/hook/claude/session-start"

    def test_supports_nested_hook_names(self, mock_proxy: dict[str, Any]) -> None:
        _invoke(
            ["claude", "sub/agent-start", "--proxy-url", mock_proxy["url"]],
            stdin='{"x": 1}',
        )
        assert mock_proxy["calls"][0]["path"] == "/hook/claude/sub/agent-start"

    def test_passes_through_empty_stdin(self, mock_proxy: dict[str, Any]) -> None:
        result = _invoke(["claude", "session-start", "--proxy-url", mock_proxy["url"]], stdin="")
        assert result.exit_code == 0
        assert mock_proxy["calls"][0]["body"] == b""

    def test_invalid_proxy_url_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, logger="ai_guard"):
            result = _invoke(
                ["claude", "session-start", "--proxy-url", "not-a-url"],
            )
        assert result.exit_code == 0
        assert any("failed to invoke hook" in rec.message for rec in caplog.records)
