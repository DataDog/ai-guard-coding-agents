"""Tests for the ai-guard installer.

Grouped by surface area:

* :class:`TestConfig`         — ``~/.ai_guard/config.env`` read/write.
* :class:`TestBackup`         — versioned backups + ``restore-state.json``.
* :class:`TestClaudeInstaller` — JSON merge, hook install/uninstall, upstream chaining.
* :class:`TestPrompt`         — tiered prompting + silent defaults.
* :class:`TestReadiness`      — TCP-poll equivalent of ``nc -z``.
* :class:`TestService`        — wrapper, templates, launchd/systemd registration.
* :class:`TestCli`            — end-to-end ``install`` / ``uninstall`` via ``CliRunner``.
* :class:`TestMultiAgent`     — a fake non-Claude :class:`AgentInstaller` plugged
  through the same generic pipeline.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import click
import pytest
from click.testing import CliRunner

from aiguard.claude.installer import (
    HOOK_EVENTS,
    ClaudeInstaller,
    _is_ai_guard_entry,
    build_hooks_section,
)
from aiguard.installer import backup, config, paths, prompt
from aiguard.installer import installer as installer_cli
from aiguard.installer.agent import AgentInstaller, Field, InstallResult
from aiguard.installer.installer import install, uninstall
from aiguard.installer.service import wrapper
from aiguard.installer.service.readiness import wait_ready
from aiguard.installer.templates import render

# ── Shared helpers ────────────────────────────────────────────────────────────

PROXY = "http://127.0.0.1:29279"
DOCKER_SETTINGS = Path(__file__).resolve().parents[2] / "docker" / "claude" / "claude-settings.json"


def _make_settings(tmp_home: Path, contents: str = "{}") -> Path:
    target = tmp_home / ".claude" / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents, encoding="utf-8")
    return target


def _make_claude_dir(tmp_home: Path) -> Path:
    d = tmp_home / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    try:
        return sock.getsockname()[1]
    finally:
        sock.close()


# ── Shared fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def stub_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the installer to take the Linux/systemd code path with no real systemctl."""
    monkeypatch.setattr(paths, "is_macos", lambda: False)
    monkeypatch.setattr(paths, "is_linux", lambda: True)

    def fake_run(args, **kwargs):
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("aiguard.installer.service.systemd_user.subprocess.run", fake_run)
    monkeypatch.setattr("aiguard.installer.service.launchd.subprocess.run", fake_run)


@pytest.fixture
def wait_ready_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aiguard.installer.installer.wait_ready", lambda *a, **kw: True)


@pytest.fixture
def claude_present(tmp_home: Path) -> Path:
    settings = tmp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{}")
    return settings


@pytest.fixture
def staged_binary(tmp_home: Path) -> Path:
    """Pretend the bootstrap installer already dropped the binary in place.

    Required for install runs that exercise service registration (the
    production install flow refuses to wire the service if there is no
    binary for the wrapper to exec).
    """
    paths.local_bin_dir().mkdir(parents=True, exist_ok=True)
    paths.binary_path().write_text("#!/bin/sh\nexit 0\n")
    paths.binary_path().chmod(0o755)
    return paths.binary_path()


# =============================================================================
# config.py
# =============================================================================


class TestConfig:
    def test_round_trip(self, tmp_home: Path) -> None:
        values = {
            "DD_API_KEY": "abc123",
            "DD_APP_KEY": "def456",
            "DD_SITE": "datadoghq.com",
            "DD_AI_GUARD_BLOCK": "True",
        }
        config.write(values)
        assert config.read() == values

    def test_file_is_mode_0600(self, tmp_home: Path) -> None:
        config.write({"DD_API_KEY": "secret"})
        mode = paths.config_env_path().stat().st_mode & 0o777
        assert mode == 0o600

    def test_values_with_special_chars_round_trip(self, tmp_home: Path) -> None:
        values = {
            "DD_SITE": "datadoghq.com",
            "DD_AI_GUARD_TAG": "value with spaces",
            "DD_AI_GUARD_QUOTE": 'has"double"quotes',
            "DD_AI_GUARD_DOLLAR": "literal $HOME",
        }
        config.write(values)
        assert config.read() == values

    def test_invalid_key_rejected(self, tmp_home: Path) -> None:
        with pytest.raises(ValueError):
            config.write({"lowercase_key": "x"})

    def test_no_temp_file_left_behind_on_success(self, tmp_home: Path) -> None:
        config.write({"DD_API_KEY": "x"})
        stragglers = list(paths.state_dir().glob(".config.env.*"))
        assert stragglers == []

    def test_read_missing_returns_empty(self, tmp_home: Path) -> None:
        assert config.read() == {}


# =============================================================================
# backup.py
# =============================================================================


class TestBackup:
    def test_snapshot_creates_backup(self, tmp_home: Path) -> None:
        src = _make_settings(tmp_home, '{"key": "value"}')
        dest = backup.snapshot("claude", src)
        assert dest is not None
        assert dest.exists()
        assert dest.read_text() == '{"key": "value"}'
        assert dest.parent == paths.backups_dir()

    def test_snapshot_missing_source_returns_none(self, tmp_home: Path) -> None:
        missing = tmp_home / ".claude" / "missing.json"
        assert backup.snapshot("claude", missing) is None

    def test_snapshot_reuses_existing_for_same_agent(self, tmp_home: Path) -> None:
        """Re-install must not overwrite the pristine snapshot.

        The first install captures the original settings.json. Subsequent
        snapshots — for files that we have since modified — would erode the
        forensic value of the backup if allowed to accumulate or rotate.
        """
        src = _make_settings(tmp_home, '{"original": true}')
        first = backup.snapshot("claude", src)
        assert first is not None

        # Modify the source file, then snapshot again.
        src.write_text('{"original": false}')
        second = backup.snapshot("claude", src)

        assert second == first
        assert first.read_text() == '{"original": true}'
        assert len(list(paths.backups_dir().glob("claude-settings.*.json"))) == 1

    def test_restore_state_round_trip(self, tmp_home: Path) -> None:
        src = _make_settings(tmp_home)
        backup.record_install("claude", src, {"ANTHROPIC_BASE_URL": "https://upstream.example/v1"})
        record = backup.load_install("claude")
        assert record is not None
        assert record["restore_data"] == {"ANTHROPIC_BASE_URL": "https://upstream.example/v1"}
        assert record["settings_path"] == str(src)
        assert backup.all_agents() == ["claude"]

    def test_restore_state_handles_empty_restore_data(self, tmp_home: Path) -> None:
        src = _make_settings(tmp_home)
        backup.record_install("claude", src, {})
        record = backup.load_install("claude")
        assert record is not None
        assert record["restore_data"] == {}

    def test_record_install_preserves_prior_restore_data_on_reinstall(
        self, tmp_home: Path
    ) -> None:
        """Re-running install must not erase a chained-upstream URL captured the first time.

        First install grabs the user's pre-existing ANTHROPIC_BASE_URL into
        restore_data. On second install the agent's current env value is
        already our proxy, so it computes restore_data={}. Without merging,
        uninstall would lose the user's real upstream.
        """
        src = _make_settings(tmp_home)
        backup.record_install("claude", src, {"ANTHROPIC_BASE_URL": "https://upstream/v1"})
        backup.record_install("claude", src, {})
        record = backup.load_install("claude")
        assert record is not None
        assert record["restore_data"] == {"ANTHROPIC_BASE_URL": "https://upstream/v1"}

    def test_clear_removes_backups_dir(self, tmp_home: Path) -> None:
        src = _make_settings(tmp_home)
        backup.snapshot("claude", src)
        backup.record_install("claude", src, {})
        backup.clear()
        assert not paths.backups_dir().exists()


# =============================================================================
# Claude installer (drift guard + install/uninstall round-trip)
# =============================================================================


class TestClaudeInstaller:
    def test_hooks_match_docker_reference(self) -> None:
        """Catch any drift between the docker-baked hook config and the in-binary copy."""
        reference = json.loads(DOCKER_SETTINGS.read_text())
        generated = {"hooks": build_hooks_section()}
        assert generated == reference

    def test_install_on_missing_file_creates_it(self, tmp_home: Path) -> None:
        _make_claude_dir(tmp_home)
        agent = ClaudeInstaller()
        result = agent.install_hooks(PROXY)

        assert result.restore_data == {}
        data = _read(result.settings_path)
        assert data["env"]["ANTHROPIC_BASE_URL"] == PROXY
        assert set(data["hooks"].keys()) == set(HOOK_EVENTS)

    def test_install_preserves_unrelated_keys(self, tmp_home: Path) -> None:
        settings = _make_claude_dir(tmp_home) / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "permissions": {"allow": ["Bash"]},
                    "model": "claude-sonnet-4-6",
                }
            )
        )

        ClaudeInstaller().install_hooks(PROXY)
        data = _read(settings)

        assert data["permissions"] == {"allow": ["Bash"]}
        assert data["model"] == "claude-sonnet-4-6"
        assert data["env"]["ANTHROPIC_BASE_URL"] == PROXY

    def test_install_captures_pre_existing_anthropic_base_url(self, tmp_home: Path) -> None:
        settings = _make_claude_dir(tmp_home) / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "env": {"ANTHROPIC_BASE_URL": "https://upstream.example/v1"},
                }
            )
        )

        result = ClaudeInstaller().install_hooks(PROXY)
        assert result.restore_data == {"ANTHROPIC_BASE_URL": "https://upstream.example/v1"}

        data = _read(settings)
        assert data["env"]["ANTHROPIC_BASE_URL"] == PROXY

    def test_detect_upstream_finds_pre_existing_value(self, tmp_home: Path) -> None:
        settings = _make_claude_dir(tmp_home) / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "env": {"ANTHROPIC_BASE_URL": "https://upstream.example/v1"},
                }
            )
        )
        assert ClaudeInstaller().detect_upstream() == "https://upstream.example/v1"

    def test_detect_upstream_returns_none_when_unset(self, tmp_home: Path) -> None:
        _make_claude_dir(tmp_home)
        assert ClaudeInstaller().detect_upstream() is None

    def test_reinstall_is_idempotent(self, tmp_home: Path) -> None:
        _make_claude_dir(tmp_home)
        ClaudeInstaller().install_hooks(PROXY)
        ClaudeInstaller().install_hooks(PROXY)

        data = _read(ClaudeInstaller().settings_path)
        # Each hook event still has exactly one ai-guard entry.
        for event in HOOK_EVENTS:
            entries = data["hooks"][event]
            ai_guard_entries = [e for e in entries if _is_ai_guard_entry(e)]
            assert len(ai_guard_entries) == 1

    def test_install_preserves_user_hooks(self, tmp_home: Path) -> None:
        user_hook = {"hooks": [{"type": "command", "command": "/usr/bin/my-tool --foo"}]}
        settings = _make_claude_dir(tmp_home) / "settings.json"
        settings.write_text(json.dumps({"hooks": {"PreToolUse": [user_hook]}}))

        ClaudeInstaller().install_hooks(PROXY)
        data = _read(settings)

        pretooluse = data["hooks"]["PreToolUse"]
        assert user_hook in pretooluse
        assert any(_is_ai_guard_entry(e) for e in pretooluse)

    def test_uninstall_removes_only_our_entries(self, tmp_home: Path) -> None:
        user_hook = {"hooks": [{"type": "command", "command": "/usr/bin/my-tool --foo"}]}
        settings = _make_claude_dir(tmp_home) / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "hooks": {"PreToolUse": [user_hook]},
                    "model": "claude-sonnet-4-6",
                }
            )
        )

        agent = ClaudeInstaller()
        agent.install_hooks(PROXY)
        agent.uninstall_hooks(restore_data={})

        data = _read(settings)
        assert data == {
            "hooks": {"PreToolUse": [user_hook]},
            "model": "claude-sonnet-4-6",
        }

    def test_uninstall_restores_chained_upstream(self, tmp_home: Path) -> None:
        settings = _make_claude_dir(tmp_home) / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "env": {"ANTHROPIC_BASE_URL": "https://upstream.example/v1"},
                }
            )
        )

        agent = ClaudeInstaller()
        result = agent.install_hooks(PROXY)
        agent.uninstall_hooks(restore_data=result.restore_data)

        data = _read(settings)
        assert data == {"env": {"ANTHROPIC_BASE_URL": "https://upstream.example/v1"}}

    def test_uninstall_removes_env_key_when_no_original(self, tmp_home: Path) -> None:
        _make_claude_dir(tmp_home)
        agent = ClaudeInstaller()
        agent.install_hooks(PROXY)
        agent.uninstall_hooks(restore_data={})

        data = _read(agent.settings_path)
        # The whole env block was synthesised by us, so it should be gone too.
        assert "env" not in data
        assert "hooks" not in data

    def test_detect_finds_settings_dir(self, tmp_home: Path) -> None:
        _make_claude_dir(tmp_home)
        assert ClaudeInstaller().detect() is not None

    def test_detect_missing_dir_returns_none(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("aiguard.claude.installer.detect_executable", lambda _: None)
        assert ClaudeInstaller().detect() is None


# =============================================================================
# prompt.py
# =============================================================================


def _run_prompt(args: dict, env_inputs: str = "") -> dict:
    """Run prompt.collect inside a Click context so click.prompt works."""
    captured: dict = {}

    @click.command()
    def cmd() -> None:
        captured.update(prompt.collect(**args))

    result = CliRunner().invoke(cmd, [], input=env_inputs)
    assert result.exit_code == 0, result.output + str(result.exception)
    captured["__stdout__"] = result.output
    return captured


class TestPrompt:
    def test_non_interactive_from_env(self) -> None:
        values = _run_prompt(
            {
                "advanced": False,
                "non_interactive": True,
                "detected_upstream": None,
                "env": {"DD_API_KEY": "k", "DD_APP_KEY": "a"},
            }
        )
        # Tier-1 secrets came from env; service has its default.
        assert values["DD_API_KEY"] == "k"
        assert values["DD_APP_KEY"] == "a"
        assert values["DD_SERVICE"] == "ai-guard"
        # Tier-2 defaults are filled in even without --advanced.
        assert values["DD_SITE"] == "datadoghq.com"
        assert values["DD_ENV"] == "prod"
        assert values["DD_AI_GUARD_PROXY_PORT"] == "29279"
        # Silent defaults are always written.
        assert values["DD_TRACE_ENABLED"] == "True"
        assert values["DD_AI_GUARD_ENABLED"] == "True"

    def test_silent_defaults_never_prompted_interactive(self) -> None:
        """In interactive mode the silent vars must NOT consume any input — if
        they did, the trailing newline would be eaten and the test would hang or
        pick up the wrong value."""
        values = _run_prompt(
            {
                "advanced": False,
                "non_interactive": False,
                "detected_upstream": None,
                "env": {},
            },
            # Exactly three inputs for the three tier-1 prompts: api, app, service.
            env_inputs="my-secret-api\nmy-secret-app\n\n",
        )
        assert values["DD_TRACE_ENABLED"] == "True"
        assert values["DD_AI_GUARD_ENABLED"] == "True"
        # Tier-1 prompts ran; tier-2 took silent defaults.
        assert values["DD_API_KEY"] == "my-secret-api"
        assert values["DD_APP_KEY"] == "my-secret-app"
        assert values["DD_SERVICE"] == "ai-guard"
        # Tier-2 defaults landed without prompting.
        assert values["DD_SITE"] == "datadoghq.com"
        assert values["DD_ENV"] == "prod"

    def test_silent_defaults_respect_env_override(self) -> None:
        """Env-supplied values for silent vars win over the hard-coded default."""
        values = _run_prompt(
            {
                "advanced": False,
                "non_interactive": True,
                "detected_upstream": None,
                "env": {
                    "DD_API_KEY": "k",
                    "DD_APP_KEY": "a",
                    "DD_TRACE_ENABLED": "False",
                    "DD_AI_GUARD_ENABLED": "False",
                },
            }
        )
        assert values["DD_TRACE_ENABLED"] == "False"
        assert values["DD_AI_GUARD_ENABLED"] == "False"

    def test_non_interactive_missing_secret_raises(self) -> None:
        with pytest.raises(prompt.MissingRequiredError) as exc:
            prompt.collect(
                advanced=False,
                non_interactive=True,
                detected_upstream=None,
                env={"DD_APP_KEY": "a"},
            )
        assert exc.value.key == "DD_API_KEY"

    def test_interactive_prompts_and_hides_secret(self) -> None:
        # Three tier-1 prompts: api (hidden), app (hidden), service (default).
        values = _run_prompt(
            {
                "advanced": False,
                "non_interactive": False,
                "detected_upstream": None,
                "env": {},
            },
            env_inputs="my-secret-api\nmy-secret-app\nmy-service\n",
        )
        assert values["DD_API_KEY"] == "my-secret-api"
        assert values["DD_APP_KEY"] == "my-secret-app"
        assert values["DD_SERVICE"] == "my-service"

        # Click suppresses echo for hide_input=True so the secret never lands in the
        # CliRunner output buffer.
        assert "my-secret-api" not in values["__stdout__"]
        assert "my-secret-app" not in values["__stdout__"]

    def test_advanced_prefills_detected_upstream_for_claude(self) -> None:
        # The Anthropic upstream field comes from ClaudeInstaller.env_fields,
        # so it only shows up when claude is one of the detected agents.
        values = _run_prompt(
            {
                "advanced": True,
                "non_interactive": True,
                "detected_upstream": "https://upstream.example/v1",
                "env": {"DD_API_KEY": "k", "DD_APP_KEY": "a"},
                "agents": [ClaudeInstaller()],
            }
        )
        assert values["DD_AI_GUARD_ANTHROPIC_UPSTREAM"] == "https://upstream.example/v1"

    def test_anthropic_upstream_field_absent_without_claude(self) -> None:
        # Without claude in the agents list, DD_AI_GUARD_ANTHROPIC_UPSTREAM is
        # not a prompt-list field and stays out of the resulting config.env
        # (the proxy will fall back to its hard-coded api.anthropic.com).
        values = _run_prompt(
            {
                "advanced": True,
                "non_interactive": True,
                "detected_upstream": "https://upstream.example/v1",
                "env": {"DD_API_KEY": "k", "DD_APP_KEY": "a"},
                "agents": [],
            }
        )
        assert "DD_AI_GUARD_ANTHROPIC_UPSTREAM" not in values

    def test_env_overrides_default_in_interactive(self) -> None:
        # Tier-1 secrets in env short-circuit their prompts; DD_SERVICE still
        # prompts because env doesn't override it. One newline accepts its default.
        values = _run_prompt(
            {
                "advanced": False,
                "non_interactive": False,
                "detected_upstream": None,
                "env": {"DD_API_KEY": "envk", "DD_APP_KEY": "enva"},
            },
            env_inputs="\n",
        )
        assert values["DD_API_KEY"] == "envk"
        assert values["DD_APP_KEY"] == "enva"
        assert values["DD_SERVICE"] == "ai-guard"

    def test_secret_prompt_uses_pwinput_on_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On a real TTY the secret prompt should route through pwinput (which
        echoes ``*`` per character). On non-TTYs we fall through to
        ``click.prompt(hide_input=True)`` — that path is exercised by every
        other test in this class via ``CliRunner``."""
        from aiguard.installer import prompt as _prompt

        monkeypatch.setattr(_prompt.sys.stdin, "isatty", lambda: True)
        calls: list[dict] = []

        def fake_pwinput(prompt: str, mask: str = "*") -> str:
            calls.append({"prompt": prompt, "mask": mask})
            return "from-pwinput"

        monkeypatch.setattr("pwinput.pwinput", fake_pwinput)

        assert _prompt._read_secret("DD_API_KEY") == "from-pwinput"
        assert calls == [{"prompt": "DD_API_KEY: ", "mask": "*"}]

    def test_secret_prompt_falls_back_to_click_off_tty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Off-TTY (tests, CI piped stdin) must not call pwinput, which would
        raise ``termios.error`` trying to put a non-tty into raw mode."""
        from aiguard.installer import prompt as _prompt

        monkeypatch.setattr(_prompt.sys.stdin, "isatty", lambda: False)

        def explode(*a, **kw):  # pragma: no cover - should never be called
            raise AssertionError("pwinput must not be called off-TTY")

        monkeypatch.setattr("pwinput.pwinput", explode)
        monkeypatch.setattr(_prompt.click, "prompt", lambda *a, **kw: "from-click")

        assert _prompt._read_secret("DD_API_KEY") == "from-click"


# =============================================================================
# readiness.py — TCP-poll equivalent of `nc -z`
# =============================================================================


class TestReadiness:
    def test_returns_true_when_port_open(self) -> None:
        port = _free_port()
        srv = socket.socket()
        srv.bind(("127.0.0.1", port))
        srv.listen()
        try:
            assert wait_ready("127.0.0.1", port, timeout=1.0)
        finally:
            srv.close()

    def test_returns_true_when_port_opens_during_wait(self) -> None:
        port = _free_port()

        def open_later():
            time.sleep(0.2)
            s = socket.socket()
            s.bind(("127.0.0.1", port))
            s.listen()
            # Hold the socket open for the duration of the test.
            time.sleep(1.0)
            s.close()

        t = threading.Thread(target=open_later, daemon=True)
        t.start()
        try:
            assert wait_ready("127.0.0.1", port, timeout=2.0, interval=0.05)
        finally:
            t.join(timeout=2.0)

    def test_returns_false_on_timeout(self) -> None:
        port = _free_port()  # nothing listening
        start = time.monotonic()
        assert not wait_ready("127.0.0.1", port, timeout=0.3, interval=0.05)
        assert time.monotonic() - start < 1.0


# =============================================================================
# service/ — wrapper, templates, launchd / systemd registration
# =============================================================================


class TestService:
    def test_wrapper_script_contents(self, tmp_home: Path) -> None:
        wrapper.write()
        target = paths.wrapper_path()
        text = target.read_text()
        assert str(paths.config_env_path()) in text
        assert str(paths.binary_path()) in text
        assert "exec" in text
        assert target.stat().st_mode & 0o777 == 0o755

    def test_wrapper_remove_is_safe_when_missing(self, tmp_home: Path) -> None:
        # Nothing to remove yet; should not raise.
        wrapper.remove()

    def test_launchd_plist_template_renders(self, tmp_home: Path) -> None:
        out = render(
            "com.datadoghq.ai-guard.plist.in",
            LABEL="com.datadoghq.ai-guard",
            WRAPPER="/home/u/.local/bin/ai-guard-service",
            LOG="/home/u/.ai_guard/ai_guard_service.log",
            HOME="/home/u",
        )
        assert "<key>Label</key>" in out
        assert "com.datadoghq.ai-guard" in out
        assert "/home/u/.local/bin/ai-guard-service" in out
        assert "/home/u/.ai_guard/ai_guard_service.log" in out
        assert "<key>RunAtLoad</key>" in out
        assert "<true/>" in out

    def test_systemd_unit_template_renders(self) -> None:
        out = render(
            "ai-guard.service.in",
            WRAPPER="/home/u/.local/bin/ai-guard-service",
            LOG="/home/u/.ai_guard/ai_guard_service.log",
        )
        assert "ExecStart=/home/u/.local/bin/ai-guard-service" in out
        assert "append:/home/u/.ai_guard/ai_guard_service.log" in out
        assert "Restart=on-failure" in out
        assert "WantedBy=default.target" in out

    def test_launchd_install_writes_service_log_path(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: the plist must point launchd at ai_guard_service.log, not
        the proxy's own rotating ai_guard.log (which the rotator would rename
        out from under launchd, breaking append)."""
        from aiguard.installer.service import launchd

        def fake_run(args, **kwargs):
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(launchd.subprocess, "run", fake_run)
        monkeypatch.setattr(launchd.os, "getuid", lambda: 501)
        launchd.install()

        plist = paths.launchd_plist_path().read_text()
        assert str(paths.service_log_file_path()) in plist
        assert "ai_guard_service.log" in plist
        # The proxy's own app-log path must NOT appear in the plist — that file
        # belongs to the rotating handler, not launchd.
        assert str(paths.log_file_path()) not in plist

    def test_systemd_install_writes_service_log_path(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiguard.installer.service import systemd_user

        def fake_run(args, **kwargs):
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(systemd_user.subprocess, "run", fake_run)
        systemd_user.install()

        unit = paths.systemd_unit_path().read_text()
        assert str(paths.service_log_file_path()) in unit
        assert str(paths.log_file_path()) not in unit

    def test_systemd_install_calls_systemctl(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiguard.installer.service import systemd_user

        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(systemd_user.subprocess, "run", fake_run)
        systemd_user.install()

        assert paths.systemd_unit_path().exists()
        assert ["systemctl", "--user", "daemon-reload"] in calls
        assert ["systemctl", "--user", "enable", "--now", "ai-guard.service"] in calls

    def test_launchd_install_calls_launchctl(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiguard.installer.service import launchd

        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(launchd.subprocess, "run", fake_run)
        monkeypatch.setattr(launchd.os, "getuid", lambda: 501)
        launchd.install()

        assert paths.launchd_plist_path().exists()
        # Bootout-then-bootstrap pattern.
        assert any(c[:2] == ["launchctl", "bootout"] for c in calls)
        assert any(c[:2] == ["launchctl", "bootstrap"] for c in calls)


# =============================================================================
# End-to-end CLI: `ai-guard install` / `ai-guard uninstall`
# =============================================================================


class TestCli:
    def test_install_non_interactive_happy_path(
        self,
        tmp_home: Path,
        stub_platform: None,
        wait_ready_ok: None,
        claude_present: Path,
        staged_binary: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DD_API_KEY", "test-api")
        monkeypatch.setenv("DD_APP_KEY", "test-app")
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

        result = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert result.exit_code == 0, result.output + str(result.exception or "")

        # Config file written with the secret values.
        env_path = paths.config_env_path()
        assert env_path.exists()
        assert env_path.stat().st_mode & 0o777 == 0o600
        content = env_path.read_text()
        assert "DD_API_KEY=test-api" in content
        assert "DD_APP_KEY=test-app" in content

        # Claude settings now point at the proxy and carry our hooks.
        data = json.loads(claude_present.read_text())
        assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:29279"
        assert "PreToolUse" in data["hooks"]

        # Service unit + wrapper exist.
        assert paths.systemd_unit_path().exists()
        assert paths.wrapper_path().exists()

    def test_install_detects_and_chains_upstream(
        self,
        tmp_home: Path,
        stub_platform: None,
        wait_ready_ok: None,
        staged_binary: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings = tmp_home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://upstream.example/v1"}})
        )

        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.setenv("DD_APP_KEY", "a")

        result = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert result.exit_code == 0, result.output + str(result.exception or "")

        env_content = paths.config_env_path().read_text()
        assert "DD_AI_GUARD_ANTHROPIC_UPSTREAM" in env_content
        assert "https://upstream.example/v1" in env_content

    def test_uninstall_leaves_only_logs(
        self,
        tmp_home: Path,
        stub_platform: None,
        wait_ready_ok: None,
        claude_present: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.setenv("DD_APP_KEY", "a")

        # Drop a fake binary + log so we can check what survives.
        paths.local_bin_dir().mkdir(parents=True, exist_ok=True)
        paths.binary_path().write_text("#!/bin/sh\nexit 0\n")
        paths.binary_path().chmod(0o755)

        paths.state_dir().mkdir(parents=True, exist_ok=True)
        paths.log_file_path().write_text("log line\n")
        (paths.log_file_path().with_suffix(".log.1")).write_text("rotated\n")
        paths.service_log_file_path().write_text("service stdout\n")
        (paths.service_log_file_path().with_suffix(".log.1")).write_text("service rotated\n")

        inst = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert inst.exit_code == 0, inst.output + str(inst.exception or "")

        uninst = CliRunner().invoke(uninstall, ["--yes", "--no-color"])
        assert uninst.exit_code == 0, uninst.output + str(uninst.exception or "")

        # Both log families survive (proxy's rotating log + launchd/systemd stdout capture)...
        assert paths.log_file_path().exists()
        assert paths.log_file_path().with_suffix(".log.1").exists()
        assert paths.service_log_file_path().exists()
        assert paths.service_log_file_path().with_suffix(".log.1").exists()
        # ...everything else is gone.
        assert not paths.config_env_path().exists()
        assert not paths.backups_dir().exists()
        assert not paths.wrapper_path().exists()
        assert not paths.binary_path().exists()
        # Hooks are gone from the settings file.
        data = json.loads(claude_present.read_text())
        assert "hooks" not in data or data["hooks"] == {}

    def test_reinstall_reuses_stored_config_without_env(
        self,
        tmp_home: Path,
        stub_platform: None,
        wait_ready_ok: None,
        claude_present: Path,
        staged_binary: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Re-running --non-interactive install with no env vars must succeed.

        Stored config.env is the source of truth on re-run; the user should
        not have to re-export their secrets every time they re-install.
        """
        monkeypatch.setenv("DD_API_KEY", "first-api")
        monkeypatch.setenv("DD_APP_KEY", "first-app")
        first = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert first.exit_code == 0, first.output + str(first.exception or "")

        monkeypatch.delenv("DD_API_KEY")
        monkeypatch.delenv("DD_APP_KEY")
        second = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert second.exit_code == 0, second.output + str(second.exception or "")

        content = paths.config_env_path().read_text()
        assert "DD_API_KEY=first-api" in content
        assert "DD_APP_KEY=first-app" in content

    def test_reinstall_env_var_overrides_stored_config(
        self,
        tmp_home: Path,
        stub_platform: None,
        wait_ready_ok: None,
        claude_present: Path,
        staged_binary: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DD_API_KEY", "first-api")
        monkeypatch.setenv("DD_APP_KEY", "first-app")
        CliRunner().invoke(install, ["--non-interactive", "--no-color"])

        monkeypatch.setenv("DD_API_KEY", "rotated-api")
        result = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert result.exit_code == 0, result.output + str(result.exception or "")

        content = paths.config_env_path().read_text()
        assert "DD_API_KEY=rotated-api" in content
        assert "DD_APP_KEY=first-app" in content

    def test_reinstall_preserves_chained_upstream_in_restore_state(
        self,
        tmp_home: Path,
        stub_platform: None,
        wait_ready_ok: None,
        staged_binary: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After re-install, restore-state must still hold the user's original upstream."""
        settings = tmp_home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://upstream.example/v1"}})
        )

        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.setenv("DD_APP_KEY", "a")
        first = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert first.exit_code == 0

        second = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert second.exit_code == 0

        record = backup.load_install("claude")
        assert record is not None
        assert record["restore_data"] == {"ANTHROPIC_BASE_URL": "https://upstream.example/v1"}

    def test_install_missing_secret_exits_non_zero(
        self,
        tmp_home: Path,
        stub_platform: None,
        wait_ready_ok: None,
        claude_present: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("DD_API_KEY", raising=False)
        monkeypatch.delenv("DD_APP_KEY", raising=False)

        result = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert result.exit_code == 2
        assert "DD_API_KEY" in result.output

    def test_install_from_source_fails_clearly_when_binary_missing(
        self,
        tmp_home: Path,
        stub_platform: None,
        wait_ready_ok: None,
        claude_present: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`uv run ai-guard install` without a built binary must exit non-zero
        with a clear error pointing at the bootstrap script — NOT silently write
        a wrapper that exec's a nonexistent path."""
        import sys as _sys

        monkeypatch.setattr(_sys, "frozen", False, raising=False)
        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.setenv("DD_APP_KEY", "a")
        # No binary at paths.binary_path() — that's the whole point of the test.
        assert not paths.binary_path().exists()

        result = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert result.exit_code == 1
        assert "no ai-guard binary" in result.output
        assert "pyinstaller" in result.output
        # The wrapper must not have been left behind pointing at a dangling path.
        assert not paths.wrapper_path().exists()

    def test_install_frozen_binary_copies_itself_to_target(
        self,
        tmp_home: Path,
        stub_platform: None,
        wait_ready_ok: None,
        claude_present: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A frozen ai-guard binary running from anywhere else should copy itself
        into place so the generated wrapper has something to exec."""
        import sys as _sys

        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.setenv("DD_APP_KEY", "a")
        assert not paths.binary_path().exists()

        # Pretend we're running as a frozen binary at a different location.
        elsewhere = tmp_home / "downloads" / "ai-guard"
        elsewhere.parent.mkdir(parents=True, exist_ok=True)
        elsewhere.write_text("#!/bin/sh\necho not actually executed\n")
        elsewhere.chmod(0o755)
        monkeypatch.setattr(_sys, "frozen", True, raising=False)
        monkeypatch.setattr(_sys, "executable", str(elsewhere))

        result = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert result.exit_code == 0, result.output + str(result.exception or "")

        # Binary is now where the wrapper expects it.
        assert paths.binary_path().exists()
        assert paths.binary_path().stat().st_mode & 0o755 == 0o755
        # And the wrapper points at it.
        assert str(paths.binary_path()) in paths.wrapper_path().read_text()


# =============================================================================
# Multi-agent regression: a non-Claude agent runs through the same pipeline.
# =============================================================================


class FakeAgent(AgentInstaller):
    """A pretend coding-agent that writes a flat JSON config and exposes its
    own upstream key. Exercises the Agent ABC end-to-end without touching the
    Claude-specific code path."""

    name = "fake"
    UPSTREAM_KEY = "FAKE_API_BASE_URL"

    def __init__(self, settings_path: Path | None = None) -> None:
        self._settings_path = settings_path or (paths.home() / ".fake-agent" / "config.json")

    @property
    def settings_path(self) -> Path:
        return self._settings_path

    def _load(self) -> dict:
        if not self._settings_path.exists():
            return {}
        return json.loads(self._settings_path.read_text(encoding="utf-8"))

    def _write(self, data: dict) -> None:
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        self._settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def detect(self) -> Path | None:
        return self._settings_path if self._settings_path.parent.exists() else None

    def detect_upstream(self) -> str | None:
        return self._load().get(self.UPSTREAM_KEY) or None

    def env_fields(self, detected_upstream: str | None) -> tuple[Field, ...]:
        # FakeAgent demonstrates a non-Anthropic agent contributing its own
        # tier-2 upstream var, distinct from DD_AI_GUARD_ANTHROPIC_UPSTREAM.
        return (
            Field(
                "DD_AI_GUARD_FAKE_UPSTREAM",
                "Upstream Fake endpoint",
                default=detected_upstream or "https://api.fake.example",
                tier=2,
            ),
        )

    def install_hooks(self, proxy_url: str) -> InstallResult:
        data = self._load()
        restore_data: dict[str, str] = {}
        prior = data.get(self.UPSTREAM_KEY)
        if prior and prior != proxy_url:
            restore_data[self.UPSTREAM_KEY] = prior
        backup_path = backup.snapshot(self.name, self._settings_path)
        data[self.UPSTREAM_KEY] = proxy_url
        data.setdefault("hooks", []).append({"command": "ai-guard hook fake event"})
        self._write(data)
        return InstallResult(
            settings_path=self._settings_path,
            backup_path=backup_path,
            restore_data=restore_data,
        )

    def uninstall_hooks(self, restore_data: dict[str, str]) -> None:
        if not self._settings_path.exists():
            return
        data = self._load()
        data["hooks"] = [
            h for h in data.get("hooks", []) if not h.get("command", "").startswith("ai-guard hook")
        ]
        if not data["hooks"]:
            data.pop("hooks", None)
        prior = restore_data.get(self.UPSTREAM_KEY)
        if prior:
            data[self.UPSTREAM_KEY] = prior
        else:
            data.pop(self.UPSTREAM_KEY, None)
        self._write(data)


@pytest.fixture
def register_fake_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(installer_cli.AGENT_CLASSES, "fake", FakeAgent)


class TestMultiAgent:
    def test_install_uninstall_round_trip_for_fake_agent(
        self,
        tmp_home: Path,
        stub_platform: None,
        wait_ready_ok: None,
        register_fake_agent: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-Claude agent runs through the full install + uninstall pipeline."""
        # Set up the fake agent's marker dir so `detect()` succeeds, and seed a
        # pre-existing upstream so we can verify chaining + restore work generically.
        fake_cfg = tmp_home / ".fake-agent" / "config.json"
        fake_cfg.parent.mkdir(parents=True, exist_ok=True)
        fake_cfg.write_text(json.dumps({"FAKE_API_BASE_URL": "https://my-fake.example/v1"}))

        # Pre-stage a fake binary at the expected location; install refuses to
        # wire the service otherwise (production install would normally have the
        # bootstrap script drop the binary in place first).
        paths.local_bin_dir().mkdir(parents=True, exist_ok=True)
        paths.binary_path().write_text("#!/bin/sh\nexit 0\n")
        paths.binary_path().chmod(0o755)

        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.setenv("DD_APP_KEY", "a")
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

        runner = CliRunner()
        result = runner.invoke(
            installer_cli.install,
            ["--non-interactive", "--no-color", "--agent", "fake"],
        )
        assert result.exit_code == 0, result.output + str(result.exception or "")

        # The fake agent contributed its own upstream Field via env_fields(),
        # so its var lands in config.env with the detected upstream pre-filled
        # as the default. Crucially, the Anthropic-specific var does NOT
        # appear because claude isn't in the agent list.
        env_content = paths.config_env_path().read_text()
        assert "DD_AI_GUARD_FAKE_UPSTREAM=https://my-fake.example/v1" in env_content
        assert "DD_AI_GUARD_ANTHROPIC_UPSTREAM" not in env_content

        # Hooks landed in the fake agent's config and the proxy URL was wired up.
        data = json.loads(fake_cfg.read_text())
        assert data["FAKE_API_BASE_URL"] == "http://127.0.0.1:29279"
        assert data["hooks"] == [{"command": "ai-guard hook fake event"}]

        # restore-state.json records the agent under its own name with the new opaque shape.
        record = backup.load_install("fake")
        assert record is not None
        assert record["restore_data"] == {"FAKE_API_BASE_URL": "https://my-fake.example/v1"}

        # Uninstall reverses everything via the same generic plumbing.
        result = runner.invoke(installer_cli.uninstall, ["--yes", "--no-color"])
        assert result.exit_code == 0, result.output + str(result.exception or "")

        data = json.loads(fake_cfg.read_text())
        assert data == {"FAKE_API_BASE_URL": "https://my-fake.example/v1"}

    def test_detect_upstream_falls_back_to_env_when_agent_has_no_value(
        self,
        tmp_home: Path,
        register_fake_agent: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If no agent reports an upstream, the env var is the final fallback."""
        fake_cfg = tmp_home / ".fake-agent" / "config.json"
        fake_cfg.parent.mkdir(parents=True, exist_ok=True)
        fake_cfg.write_text("{}")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://from-env.example/v1")

        agents = [FakeAgent()]
        assert installer_cli._detect_upstream(agents) == "https://from-env.example/v1"

    def test_unknown_agents_error_lists_registered_agents(
        self,
        tmp_home: Path,
        register_fake_agent: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The 'no agents detected' message names every registered agent, not just claude."""
        # Make sure no agent's detect() succeeds.
        monkeypatch.setattr("aiguard.claude.installer.detect_executable", lambda _: None)

        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.setenv("DD_APP_KEY", "a")

        result = CliRunner().invoke(installer_cli.install, ["--non-interactive", "--no-color"])
        assert result.exit_code == 1
        assert "claude" in result.output
        assert "fake" in result.output
