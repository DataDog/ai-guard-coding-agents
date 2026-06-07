# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Unit tests for the ``ai-guard`` CLI.

Two surfaces:

  * :class:`TestHookCli` — the ``ai-guard hook AGENT HOOK`` command. It selects
    the agent's handler and runs it in-process; errors are swallowed (logged via
    ``logger.exception``, exit 0) so a failing hook never breaks the calling
    agent's command flow.
  * :class:`TestDdtraceDeferred` — importing ``aiguard.cli`` must not pull in
    ddtrace (credentials/log setup run before the client is built).
  * :class:`TestMainCli` / :class:`TestSetupLogging` — the top-level entry point
    and its log-file plumbing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from aiguard.hooks.hooks import hook
from tests.transcripts import TranscriptWriter, assistant_tool_use, user_text

# ── ai-guard hook AGENT HOOK ──────────────────────────────────────────────────


def _invoke(args: list[str], stdin: bytes = b"{}") -> Any:
    return CliRunner().invoke(hook, args, input=stdin)


@pytest.mark.usefixtures("tmp_home", "fake_endpoint_id")
class TestHookCli:
    """The ``hook`` command dispatches stdin to the agent's handler in-process."""

    def test_dispatches_to_claude_handler_and_allows(self, fake_ai_guard) -> None:
        result = _invoke(["claude", "SessionStart"], json.dumps({"session_id": "s1"}).encode())
        assert result.exit_code == 0, result.output
        assert result.output == ""

    def test_writes_block_decision_to_stdout(
        self, transcripts: TranscriptWriter, fake_ai_guard
    ) -> None:
        from ddtrace.appsec.ai_guard import AIGuardAbortError

        path = transcripts.write_main(
            "s1", [assistant_tool_use("tu1", "Bash", {"command": "rm -rf /"})]
        )
        fake_ai_guard.queue_abort(
            AIGuardAbortError(action="DENY", reason="prompt_injection", tags=["t"])
        )
        event = {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "transcript_path": path,
            "tool_name": "Bash",
            "tool_use_id": "tu1",
        }
        result = _invoke(["claude", "PreToolUse"], json.dumps(event).encode())

        assert result.exit_code == 0
        body = json.loads(result.output)
        assert body["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_unknown_agent_is_noop_and_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, logger="ai_guard"):
            result = _invoke(["bogus", "SessionStart"])
        assert result.exit_code == 0
        assert result.output == ""
        assert any("no hook handler registered" in rec.message for rec in caplog.records)

    def test_handler_construction_error_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def _boom(meta: Any = None) -> Any:
            raise RuntimeError("no credentials")

        monkeypatch.setattr("aiguard.claude.handler.new_ai_guard_client", _boom)
        with caplog.at_level(logging.ERROR, logger="ai_guard"):
            result = _invoke(["claude", "SessionStart"])

        assert result.exit_code == 0
        assert result.output == ""
        assert any("failed to invoke hook" in rec.message for rec in caplog.records)

    def test_invalid_json_payload_is_tolerated(self, fake_ai_guard) -> None:
        result = _invoke(["claude", "PreToolUse"], b"{not json")
        assert result.exit_code == 0
        assert result.output == ""

    def test_empty_stdin_is_tolerated(self, fake_ai_guard) -> None:
        result = _invoke(["claude", "SessionStart"], b"")
        assert result.exit_code == 0

    def test_block_env_var_disables_blocking(
        self, transcripts: TranscriptWriter, fake_ai_guard, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DD_AI_GUARD_BLOCK", "false")
        path = transcripts.write_main("s1", [user_text("hi")])
        event = {"session_id": "s1", "transcript_path": path, "tool_name": "Bash"}
        result = _invoke(["claude", "PreToolUse"], json.dumps(event).encode())
        assert result.exit_code == 0
        assert fake_ai_guard.calls[0][1].get("block") is False

    def test_blocks_by_default(self, transcripts: TranscriptWriter, fake_ai_guard) -> None:
        path = transcripts.write_main("s1", [user_text("hi")])
        event = {"session_id": "s1", "transcript_path": path, "tool_name": "Bash"}
        result = _invoke(["claude", "PreToolUse"], json.dumps(event).encode())
        assert result.exit_code == 0
        assert fake_ai_guard.calls[0][1].get("block") is True


# ── ai-guard (top-level) ──────────────────────────────────────────────────────


class TestDdtraceDeferred:
    """Importing the CLI must not pull in ddtrace.

    ddtrace reads DD_* / OTEL_ env at import time, and the CLI loads credentials
    (config.env + keychain) into the environment before the AI Guard client is
    built. That ordering only holds if nothing on the ``aiguard.cli`` import
    path imports ddtrace eagerly — the handler that needs it is loaded lazily by
    the hook command. Run in a clean subprocess since the test process already
    has ddtrace loaded via other modules.
    """

    def test_importing_cli_does_not_import_ddtrace(self) -> None:
        import subprocess
        import sys

        code = "import aiguard.cli, sys; sys.exit('ddtrace' in sys.modules)"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert result.returncode == 0, (
            "ddtrace was imported just by importing aiguard.cli — credential and "
            "logging setup would then run too late.\n" + result.stderr
        )


class TestMainCli:
    def test_version(self) -> None:
        from aiguard import __version__
        from aiguard.cli import main

        result = CliRunner().invoke(main, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_help_lists_hook_subcommand(self) -> None:
        from aiguard.cli import main

        result = CliRunner().invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "hook" in result.output


class TestSetupLogging:
    def test_none_attaches_null_handler(self) -> None:
        from aiguard.cli import _setup_logging

        logger = logging.getLogger("ai_guard")
        before = list(logger.handlers)
        try:
            _setup_logging(None, "DEBUG")
            assert any(isinstance(h, logging.NullHandler) for h in logger.handlers)
        finally:
            logger.handlers = before

    def test_writes_file(self, tmp_path: Path) -> None:
        from aiguard.cli import _setup_logging

        log_file = tmp_path / "x" / "ai.log"
        logger = logging.getLogger("ai_guard")
        before = list(logger.handlers)
        before_level = logger.level
        try:
            _setup_logging(str(log_file), "DEBUG")
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

    def test_respects_log_level(self, tmp_path: Path) -> None:
        from aiguard.cli import _setup_logging

        log_file = tmp_path / "ai.log"
        logger = logging.getLogger("ai_guard")
        before = list(logger.handlers)
        before_level = logger.level
        try:
            _setup_logging(str(log_file), "warning")  # case-insensitive
            assert logger.level == logging.WARNING
            logger.debug("debug line")
            logger.warning("warning line")
            for h in logger.handlers:
                h.flush()
            text = log_file.read_text()
            assert "debug line" not in text
            assert "warning line" in text
        finally:
            for h in list(logger.handlers):
                if h not in before:
                    logger.removeHandler(h)
                    h.close()
            logger.setLevel(before_level)
