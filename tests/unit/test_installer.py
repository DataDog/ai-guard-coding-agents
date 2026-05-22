"""Tests for ``src/aiguard/installer/``.

Grouped by code surface — class layout mirrors the modules under test:

* :class:`TestClaudeInstaller` (``aiguard/claude/installer.py``) — JSON merge,
  hook install/uninstall, upstream chaining.
* :class:`TestCollectFields`   (``aiguard/installer/installer.py``) — tiered
  prompting + silent defaults in :func:`_collect_fields`.
* :class:`TestUiSecretEntry`   (``aiguard/installer/ui.py``) — secret-prompt
  TTY-vs-pipe routing in :func:`read_secret`.
* :class:`TestService`         (``aiguard/installer/service/``) — wrapper
  script, templates, launchd/systemd registration, log-tail dispatch.
* :class:`TestCli`             (``aiguard/installer/installer.py`` CLI) —
  end-to-end ``install`` / ``uninstall`` via ``CliRunner``.
* :class:`TestMultiAgent`      — a fake non-Claude :class:`AgentInstaller`
  plugged through the same generic pipeline.

Pure utility helpers (``wait_ready``, ``atomic_write``, platform predicates)
live in :mod:`tests.unit.test_utils` to match their home in
:mod:`aiguard.utils`.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import click
import pytest
from click.testing import CliRunner

from aiguard import paths, utils
from aiguard.claude.installer import (
    HOOK_EVENTS,
    ClaudeInstaller,
    _is_ai_guard_entry,
    build_hooks_section,
)
from aiguard.installer import installer as installer_cli
from aiguard.installer import ui
from aiguard.installer.agent import AgentInstaller, Field
from aiguard.installer.installer import install, uninstall
from aiguard.installer.service import wrapper
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


# ── Shared fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def stub_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the installer to take the Linux/systemd code path with no real systemctl."""
    monkeypatch.setattr(utils, "is_macos", lambda: False)
    monkeypatch.setattr(utils, "is_linux", lambda: True)

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
# aiguard/claude/installer.py — drift guard + install/uninstall round-trip
# =============================================================================


class TestClaudeInstaller:
    def test_hooks_match_docker_reference(self) -> None:
        """Catch any drift between the docker-baked hook config and the in-binary copy."""
        reference = json.loads(DOCKER_SETTINGS.read_text())
        generated = {"hooks": build_hooks_section()}
        assert generated == reference

    def test_install_on_missing_file_creates_it(self, tmp_home: Path) -> None:
        _make_claude_dir(tmp_home)
        settings_path = paths.claude_settings_path()
        updated = ClaudeInstaller().install(PROXY)

        assert updated == [settings_path]
        data = _read(settings_path)
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

        ClaudeInstaller().install(PROXY)
        data = _read(settings)

        assert data["permissions"] == {"allow": ["Bash"]}
        assert data["model"] == "claude-sonnet-4-6"
        assert data["env"]["ANTHROPIC_BASE_URL"] == PROXY

    def test_install_overwrites_pre_existing_anthropic_base_url(self, tmp_home: Path) -> None:
        """install() points env.ANTHROPIC_BASE_URL at the proxy regardless of prior value.

        The user's pre-existing upstream is surfaced separately via
        :meth:`ClaudeInstaller.env_fields` (the prompt picks it up as the
        default for ``DD_AI_GUARD_ANTHROPIC_UPSTREAM``) and ultimately
        restored from ``config.env`` on uninstall.
        """
        settings = _make_claude_dir(tmp_home) / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "env": {"ANTHROPIC_BASE_URL": "https://upstream.example/v1"},
                }
            )
        )

        ClaudeInstaller().install(PROXY)
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
        assert ClaudeInstaller()._detect_upstream() == "https://upstream.example/v1"

    def test_detect_upstream_returns_none_when_unset(self, tmp_home: Path) -> None:
        _make_claude_dir(tmp_home)
        assert ClaudeInstaller()._detect_upstream() is None

    def test_reinstall_is_idempotent(self, tmp_home: Path) -> None:
        _make_claude_dir(tmp_home)
        ClaudeInstaller().install(PROXY)
        ClaudeInstaller().install(PROXY)

        data = _read(paths.claude_settings_path())
        # Each hook event still has exactly one ai-guard entry.
        for event in HOOK_EVENTS:
            entries = data["hooks"][event]
            ai_guard_entries = [e for e in entries if _is_ai_guard_entry(e)]
            assert len(ai_guard_entries) == 1

    def test_install_preserves_user_hooks(self, tmp_home: Path) -> None:
        user_hook = {"hooks": [{"type": "command", "command": "/usr/bin/my-tool --foo"}]}
        settings = _make_claude_dir(tmp_home) / "settings.json"
        settings.write_text(json.dumps({"hooks": {"PreToolUse": [user_hook]}}))

        ClaudeInstaller().install(PROXY)
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
        agent.install(PROXY)
        agent.uninstall()

        data = _read(settings)
        assert data == {
            "hooks": {"PreToolUse": [user_hook]},
            "model": "claude-sonnet-4-6",
        }

    def test_uninstall_restores_chained_upstream_from_config(self, tmp_home: Path) -> None:
        """``DD_AI_GUARD_ANTHROPIC_UPSTREAM`` in config.env is what uninstall reads.

        The install flow captures the user's pre-existing upstream into
        ``config.env`` (via the prompt default), so the uninstall path can be
        purely settings-driven and the agent module never has to remember the
        original value itself.
        """
        from aiguard.storage import save_config

        settings = _make_claude_dir(tmp_home) / "settings.json"
        settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": PROXY}}))
        save_config({"DD_AI_GUARD_ANTHROPIC_UPSTREAM": "https://upstream.example/v1"})

        ClaudeInstaller().uninstall()

        data = _read(settings)
        assert data == {"env": {"ANTHROPIC_BASE_URL": "https://upstream.example/v1"}}

    def test_uninstall_removes_env_key_when_no_original(self, tmp_home: Path) -> None:
        _make_claude_dir(tmp_home)
        agent = ClaudeInstaller()
        agent.install(PROXY)
        agent.uninstall()

        data = _read(paths.claude_settings_path())
        # The whole env block was synthesised by us, so it should be gone too.
        assert "env" not in data
        assert "hooks" not in data

    def test_detect_finds_settings_dir(self, tmp_home: Path) -> None:
        _make_claude_dir(tmp_home)
        # settings.json doesn't exist yet but ~/.claude does — the executable
        # check is what carries the truthy result here when one is on PATH.
        # Either way, the agent is considered "installed".
        assert ClaudeInstaller().detect() is True

    def test_detect_missing_dir_returns_false(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("aiguard.claude.installer.detect_executable", lambda _: None)
        assert ClaudeInstaller().detect() is False


# =============================================================================
# aiguard/installer/installer.py:_collect_fields + aiguard/installer/ui.py
# =============================================================================


def _run_prompt(args: dict, env_inputs: str = "") -> dict:
    """Run ``installer._collect_fields`` inside a Click context so click.prompt works."""
    args.setdefault("agents", [])
    captured: dict = {}

    @click.command()
    def cmd() -> None:
        captured.update(installer_cli._collect_fields(**args))

    result = CliRunner().invoke(cmd, [], input=env_inputs)
    assert result.exit_code == 0, result.output + str(result.exception)
    captured["__stdout__"] = result.output
    return captured


class TestCollectFields:
    def test_non_interactive_from_env(self) -> None:
        values = _run_prompt(
            {
                "advanced": False,
                "non_interactive": True,
                "env": {"DD_API_KEY": "k", "DD_APP_KEY": "a", "DD_SERVICE": "ai-guard"},
            }
        )
        # Tier-1 secrets came from env; service was provided too.
        assert values["DD_API_KEY"] == "k"
        assert values["DD_APP_KEY"] == "a"
        assert values["DD_SERVICE"] == "ai-guard"
        # Tier-1/2 defaults are filled in even without --advanced.
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
                "env": {},
            },
            # Six tier-1 prompts: site, api, app, env, service, version.
            env_inputs="\nmy-secret-api\nmy-secret-app\n\nai-guard\n\n",
        )
        assert values["DD_TRACE_ENABLED"] == "True"
        assert values["DD_AI_GUARD_ENABLED"] == "True"
        # Tier-1 prompts ran; tier-2 took silent defaults.
        assert values["DD_API_KEY"] == "my-secret-api"
        assert values["DD_APP_KEY"] == "my-secret-app"
        assert values["DD_SERVICE"] == "ai-guard"
        # Defaults accepted for site/env.
        assert values["DD_SITE"] == "datadoghq.com"
        assert values["DD_ENV"] == "prod"

    def test_silent_defaults_respect_env_override(self) -> None:
        """Env-supplied values for silent vars win over the hard-coded default."""
        values = _run_prompt(
            {
                "advanced": False,
                "non_interactive": True,
                "env": {
                    "DD_API_KEY": "k",
                    "DD_APP_KEY": "a",
                    "DD_SERVICE": "s",
                    "DD_TRACE_ENABLED": "False",
                    "DD_AI_GUARD_ENABLED": "False",
                },
            }
        )
        assert values["DD_TRACE_ENABLED"] == "False"
        assert values["DD_AI_GUARD_ENABLED"] == "False"

    def test_non_interactive_missing_secret_raises(self) -> None:
        with pytest.raises(installer_cli.MissingRequiredError) as exc:
            installer_cli._collect_fields(
                advanced=False,
                non_interactive=True,
                env={"DD_APP_KEY": "a"},
                agents=[],
            )
        assert exc.value.key == "DD_API_KEY"

    def test_interactive_prompts_and_hides_secret(self) -> None:
        # Six tier-1 prompts: site, api (hidden), app (hidden), env, service, version.
        values = _run_prompt(
            {
                "advanced": False,
                "non_interactive": False,
                "env": {},
            },
            env_inputs="\nmy-secret-api\nmy-secret-app\n\nmy-service\n\n",
        )
        assert values["DD_API_KEY"] == "my-secret-api"
        assert values["DD_APP_KEY"] == "my-secret-app"
        assert values["DD_SERVICE"] == "my-service"

        # Click suppresses echo for hide_input=True so the secret never lands in the
        # CliRunner output buffer.
        assert "my-secret-api" not in values["__stdout__"]
        assert "my-secret-app" not in values["__stdout__"]

    def test_advanced_prefills_detected_upstream_for_claude(self, tmp_home: Path) -> None:
        # ClaudeInstaller.env_fields reads settings.json directly and uses any
        # pre-existing ANTHROPIC_BASE_URL as the default for the prompt field.
        settings = _make_claude_dir(tmp_home) / "settings.json"
        settings.write_text(
            json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://upstream.example/v1"}})
        )

        values = _run_prompt(
            {
                "advanced": True,
                "non_interactive": True,
                "env": {"DD_API_KEY": "k", "DD_APP_KEY": "a", "DD_SERVICE": "s"},
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
                "env": {"DD_API_KEY": "k", "DD_APP_KEY": "a", "DD_SERVICE": "s"},
                "agents": [],
            }
        )
        assert "DD_AI_GUARD_ANTHROPIC_UPSTREAM" not in values

    def test_env_overrides_default_in_interactive(self) -> None:
        # Env-supplied values surface as the prompt default (masked for secrets),
        # so an empty line accepts the env value. DD_SERVICE has no default in env
        # so a real value must be typed.
        values = _run_prompt(
            {
                "advanced": False,
                "non_interactive": False,
                "env": {"DD_API_KEY": "envk", "DD_APP_KEY": "enva"},
            },
            # site, api, app, env, service, version
            env_inputs="\n\n\n\nai-guard\n\n",
        )
        assert values["DD_API_KEY"] == "envk"
        assert values["DD_APP_KEY"] == "enva"
        assert values["DD_SERVICE"] == "ai-guard"


class TestUiSecretEntry:
    """``aiguard.installer.ui.read_secret`` — TTY-vs-pipe routing."""

    def test_uses_pwinput_on_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On a real TTY the secret prompt should route through pwinput (which
        echoes ``*`` per character). On non-TTYs we fall through to
        ``click.prompt(hide_input=True)`` — that path is exercised by every
        other test that goes via ``CliRunner``."""
        monkeypatch.setattr(ui.sys.stdin, "isatty", lambda: True)
        calls: list[dict] = []

        def fake_pwinput(prompt: str, mask: str = "*") -> str:
            calls.append({"prompt": prompt, "mask": mask})
            return "from-pwinput"

        monkeypatch.setattr("pwinput.pwinput", fake_pwinput)

        assert ui.read_secret("DD_API_KEY") == "from-pwinput"
        assert calls == [{"prompt": "DD_API_KEY: ", "mask": "*"}]

    def test_falls_back_to_click_off_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Off-TTY (tests, CI piped stdin) must not call pwinput, which would
        raise ``termios.error`` trying to put a non-tty into raw mode."""
        monkeypatch.setattr(ui.sys.stdin, "isatty", lambda: False)

        def explode(*a, **kw):  # pragma: no cover - should never be called
            raise AssertionError("pwinput must not be called off-TTY")

        monkeypatch.setattr("pwinput.pwinput", explode)
        monkeypatch.setattr(ui.click, "prompt", lambda *a, **kw: "from-click")

        assert ui.read_secret("DD_API_KEY") == "from-click"


# =============================================================================
# aiguard/installer/service/ — wrapper, templates, launchd/systemd, log access
# =============================================================================


class TestService:
    def test_wrapper_script_contents(self, tmp_home: Path) -> None:
        wrapper.write()
        target = paths.wrapper_path()
        text = target.read_text()
        assert str(paths.config_env_path()) in text
        assert str(paths.binary_path()) in text
        # Linux exec path; macOS pipes through logger.
        assert "exec" in text
        assert "logger -t ai-guard" in text
        assert target.stat().st_mode & 0o777 == 0o755

    def test_wrapper_remove_is_safe_when_missing(self, tmp_home: Path) -> None:
        # Nothing to remove yet; should not raise.
        wrapper.remove()

    def test_launchd_plist_template_renders(self) -> None:
        out = render(
            "com.datadoghq.ai-guard.plist.in",
            LABEL="com.datadoghq.ai-guard",
            WRAPPER="/home/u/.local/bin/ai-guard-service",
            HOME="/home/u",
            SOCKET_NAME="Listener",
            HOST="127.0.0.1",
            PORT="29279",
        )
        assert "<key>Label</key>" in out
        assert "com.datadoghq.ai-guard" in out
        assert "/home/u/.local/bin/ai-guard-service" in out
        # Socket-activated: launchd opens the port and hands it to the
        # service on demand. No RunAtLoad / KeepAlive should remain.
        assert "<key>Sockets</key>" in out
        assert "<key>Listener</key>" in out
        assert "<string>127.0.0.1</string>" in out
        assert "<string>29279</string>" in out
        assert "RunAtLoad" not in out
        assert "KeepAlive" not in out
        # No log-file paths land in the plist anymore — the wrapper pipes
        # through ``logger -t ai-guard`` into the unified log.
        assert "StandardOutPath" not in out
        assert "StandardErrorPath" not in out

    def test_systemd_unit_template_renders(self) -> None:
        out = render(
            "ai-guard.service.in",
            WRAPPER="/home/u/.local/bin/ai-guard-service",
            SOCKET_NAME="ai-guard.socket",
        )
        assert "ExecStart=/home/u/.local/bin/ai-guard-service" in out
        # journald captures stdout/stderr — no on-disk log file path.
        assert "StandardOutput=journal" in out
        assert "Restart=on-failure" in out
        # Service is socket-activated; the socket unit is what gets enabled,
        # so the service no longer carries [Install]/WantedBy.
        assert "Requires=ai-guard.socket" in out
        assert "WantedBy" not in out
        assert "append:" not in out

    def test_systemd_socket_template_renders(self) -> None:
        out = render(
            "ai-guard.socket.in",
            HOST="127.0.0.1",
            PORT="29279",
            SERVICE_NAME="ai-guard.service",
        )
        assert "[Socket]" in out
        assert "ListenStream=127.0.0.1:29279" in out
        assert "Service=ai-guard.service" in out
        assert "WantedBy=sockets.target" in out

    def test_launchd_install_writes_no_log_path(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: the plist must not point launchd at any on-disk log file.

        Service stdout/stderr is piped through ``logger -t ai-guard`` by the
        wrapper into the macOS unified log. A bare log file path in the plist
        would re-introduce a custom rotation surface we've explicitly removed.
        """
        from aiguard.installer.service import launchd

        def fake_run(args, **kwargs):
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(launchd.subprocess, "run", fake_run)
        monkeypatch.setattr(launchd.os, "getuid", lambda: 501)
        launchd.install()

        plist = paths.launchd_plist_path().read_text()
        # The proxy's own app-log path must NOT appear in the plist — that file
        # belongs to the rotating handler, not launchd.
        assert str(paths.log_file_path()) not in plist
        assert "StandardOutPath" not in plist
        assert "StandardErrorPath" not in plist

    def test_systemd_install_writes_no_log_path(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiguard.installer.service import systemd_user

        def fake_run(args, **kwargs):
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(systemd_user.subprocess, "run", fake_run)
        systemd_user.install()

        unit = paths.systemd_unit_path().read_text()
        assert "StandardOutput=journal" in unit
        # No on-disk capture surface should leak into the unit.
        assert str(paths.log_file_path()) not in unit
        assert "append:" not in unit

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

        # Both units land on disk, and systemd enables the SOCKET (not the
        # service) so requests trigger socket activation.
        assert paths.systemd_unit_path().exists()
        assert paths.systemd_socket_path().exists()
        assert ["systemctl", "--user", "daemon-reload"] in calls
        assert ["systemctl", "--user", "enable", "--now", "ai-guard.socket"] in calls

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

    # ── Log access (owned by each backend; manager just dispatches) ───────────

    def test_launchd_log_hint_is_unified_log_reader(self) -> None:
        from aiguard.installer.service import launchd

        hint = launchd.log_hint()
        assert hint.startswith("log show")
        assert "ai-guard" in hint

    def test_systemd_log_hint_is_journalctl(self) -> None:
        from aiguard.installer.service import systemd_user

        hint = systemd_user.log_hint()
        assert hint.startswith("journalctl --user")
        assert "ai-guard.service" in hint

    def test_manager_log_hint_dispatches_per_platform(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiguard.installer.service import manager

        monkeypatch.setattr(utils, "is_macos", lambda: False)
        monkeypatch.setattr(utils, "is_linux", lambda: True)
        assert manager.log_hint().startswith("journalctl --user")

        monkeypatch.setattr(utils, "is_macos", lambda: True)
        monkeypatch.setattr(utils, "is_linux", lambda: False)
        assert manager.log_hint().startswith("log show")

    def test_systemd_tail_log_returns_command_and_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiguard.installer.service import systemd_user

        def fake_run(args, **kwargs):
            return MagicMock(returncode=0, stdout="line one\nline two\n", stderr="")

        monkeypatch.setattr(systemd_user.subprocess, "run", fake_run)
        title, body = systemd_user.tail_log(lines=25)
        assert "journalctl" in title
        assert "-n 25" in title
        assert body == "line one\nline two\n"

    def test_launchd_tail_log_returns_command_and_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiguard.installer.service import launchd

        def fake_run(args, **kwargs):
            return MagicMock(returncode=0, stdout="msg one\n", stderr="")

        monkeypatch.setattr(launchd.subprocess, "run", fake_run)
        title, body = launchd.tail_log()
        assert title.startswith("log show")
        assert body == "msg one\n"


# =============================================================================
# aiguard/installer/installer.py — end-to-end `install` / `uninstall` CLI
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
        monkeypatch.setenv("DD_SERVICE", "test-service")
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
        monkeypatch.setenv("DD_SERVICE", "ai-guard")

        result = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert result.exit_code == 0, result.output + str(result.exception or "")

        env_content = paths.config_env_path().read_text()
        assert "DD_AI_GUARD_ANTHROPIC_UPSTREAM" in env_content
        assert "https://upstream.example/v1" in env_content

    def test_uninstall_leaves_only_app_log(
        self,
        tmp_home: Path,
        stub_platform: None,
        wait_ready_ok: None,
        claude_present: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After uninstall, only the proxy's rotating app log survives on disk.

        Service stdout/stderr lives in journald (Linux) or the unified log
        (macOS) — both outlive ``ai-guard uninstall`` independently and aren't
        under our state dir.
        """
        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.setenv("DD_APP_KEY", "a")
        monkeypatch.setenv("DD_SERVICE", "ai-guard")

        # Drop a fake binary + log so we can check what survives.
        paths.local_bin_dir().mkdir(parents=True, exist_ok=True)
        paths.binary_path().write_text("#!/bin/sh\nexit 0\n")
        paths.binary_path().chmod(0o755)

        paths.state_dir().mkdir(parents=True, exist_ok=True)
        paths.log_file_path().write_text("log line\n")
        (paths.log_file_path().with_suffix(".log.1")).write_text("rotated\n")

        inst = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert inst.exit_code == 0, inst.output + str(inst.exception or "")

        uninst = CliRunner().invoke(uninstall, ["--yes", "--no-color"])
        assert uninst.exit_code == 0, uninst.output + str(uninst.exception or "")

        # The proxy's rotating app log family survives...
        assert paths.log_file_path().exists()
        assert paths.log_file_path().with_suffix(".log.1").exists()
        # ...everything else is gone.
        assert not paths.config_env_path().exists()
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
        monkeypatch.setenv("DD_SERVICE", "first-service")
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
        monkeypatch.setenv("DD_SERVICE", "first-service")
        CliRunner().invoke(install, ["--non-interactive", "--no-color"])

        monkeypatch.setenv("DD_API_KEY", "rotated-api")
        result = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert result.exit_code == 0, result.output + str(result.exception or "")

        content = paths.config_env_path().read_text()
        assert "DD_API_KEY=rotated-api" in content
        assert "DD_APP_KEY=first-app" in content

    def test_reinstall_preserves_chained_upstream_in_config(
        self,
        tmp_home: Path,
        stub_platform: None,
        wait_ready_ok: None,
        staged_binary: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After re-install, config.env must still hold the user's original upstream.

        The agent's settings.json already points at our proxy by then, so a
        naive re-detection would compute an empty upstream — but the prompt
        layer reuses the stored ``DD_AI_GUARD_ANTHROPIC_UPSTREAM`` from
        config.env, which uninstall later reads back to restore the chain.
        """
        settings = tmp_home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://upstream.example/v1"}})
        )

        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.setenv("DD_APP_KEY", "a")
        monkeypatch.setenv("DD_SERVICE", "ai-guard")
        first = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert first.exit_code == 0

        second = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert second.exit_code == 0

        content = paths.config_env_path().read_text()
        assert "DD_AI_GUARD_ANTHROPIC_UPSTREAM" in content
        assert "https://upstream.example/v1" in content

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
        monkeypatch.setenv("DD_SERVICE", "ai-guard")
        # No binary at paths.binary_path() — that's the whole point of the test.
        assert not paths.binary_path().exists()

        result = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert result.exit_code == 1
        assert "no AI Guard binary" in result.output
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
        monkeypatch.setenv("DD_SERVICE", "ai-guard")
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
# Multi-agent regression — a non-Claude AgentInstaller through the same pipeline
# =============================================================================


class FakeAgent(AgentInstaller):
    """A pretend coding-agent that writes a flat JSON config and exposes its
    own upstream key. Exercises the Agent ABC end-to-end without touching the
    Claude-specific code path."""

    name = "fake"
    UPSTREAM_KEY = "FAKE_API_BASE_URL"

    def __init__(self, settings_path: Path | None = None) -> None:
        self._settings_path = settings_path or (paths.home() / ".fake-agent" / "config.json")

    def _load(self) -> dict:
        if not self._settings_path.exists():
            return {}
        return json.loads(self._settings_path.read_text(encoding="utf-8"))

    def _write(self, data: dict) -> None:
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        self._settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def detect(self) -> bool:
        return self._settings_path.parent.exists()

    def env_fields(self) -> tuple[Field, ...]:
        # FakeAgent demonstrates a non-Anthropic agent contributing its own
        # tier-2 upstream var, sourced from its own on-disk config.
        return (
            Field(
                "DD_AI_GUARD_FAKE_UPSTREAM",
                "Upstream Fake endpoint",
                default=self._load().get(self.UPSTREAM_KEY) or "https://api.fake.example",
                tier=2,
            ),
        )

    def install(self, proxy_url: str) -> list[Path]:
        data = self._load()
        data[self.UPSTREAM_KEY] = proxy_url
        data.setdefault("hooks", []).append({"command": "ai-guard hook fake event"})
        self._write(data)
        return [self._settings_path]

    def uninstall(self) -> list[Path]:
        if not self._settings_path.exists():
            return []
        from aiguard.storage import load_config

        data = self._load()
        data["hooks"] = [
            h for h in data.get("hooks", []) if not h.get("command", "").startswith("ai-guard hook")
        ]
        if not data["hooks"]:
            data.pop("hooks", None)
        prior = load_config().get("DD_AI_GUARD_FAKE_UPSTREAM", "")
        if prior:
            data[self.UPSTREAM_KEY] = prior
        else:
            data.pop(self.UPSTREAM_KEY, None)
        self._write(data)
        return [self._settings_path]


@pytest.fixture
def register_fake_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        installer_cli,
        "SUPPORTED_AGENTS",
        [*installer_cli.SUPPORTED_AGENTS, FakeAgent()],
    )


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
        monkeypatch.setenv("DD_SERVICE", "ai-guard")
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

        # Uninstall reverses everything via the same generic plumbing — the
        # captured upstream comes back from config.env.
        result = runner.invoke(installer_cli.uninstall, ["--yes", "--no-color"])
        assert result.exit_code == 0, result.output + str(result.exception or "")

        data = json.loads(fake_cfg.read_text())
        assert data == {"FAKE_API_BASE_URL": "https://my-fake.example/v1"}

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
        monkeypatch.setenv("DD_SERVICE", "ai-guard")

        result = CliRunner().invoke(installer_cli.install, ["--non-interactive", "--no-color"])
        assert result.exit_code == 1
        assert "Claude Code" in result.output
        assert "fake" in result.output
