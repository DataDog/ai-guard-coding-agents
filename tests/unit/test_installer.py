"""Tests for ``src/aiguard/installer/``.

Grouped by code surface — class layout mirrors the modules under test:

* :class:`TestClaudeInstaller` (``aiguard/claude/installer.py``) — JSON merge,
  hook install/uninstall, legacy proxy-redirect cleanup.
* :class:`TestCollectFields`   (``aiguard/installer/installer.py``) — tiered
  prompting + silent defaults in :func:`_collect_fields`.
* :class:`TestUiSecretEntry`   (``aiguard/installer/ui.py``) — secret-prompt
  TTY-vs-pipe routing in :func:`read_secret`.
* :class:`TestService`         (``aiguard/installer/service/``) — teardown of a
  legacy proxy service (wrapper + launchd/systemd units).
* :class:`TestCli`             (``aiguard/installer/installer.py`` CLI) —
  end-to-end ``install`` / ``uninstall`` via ``CliRunner``.
* :class:`TestMultiAgent`      — a fake non-Claude :class:`AgentInstaller`
  plugged through the same generic pipeline.

Pure utility helpers (``atomic_write``, platform predicates) live in
:mod:`tests.unit.test_utils` to match their home in :mod:`aiguard.utils`.
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
from aiguard.installer.agent import AgentInstaller, Field, Tier
from aiguard.installer.installer import install, uninstall
from aiguard.installer.service import wrapper
from aiguard.storage import save_config

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
def claude_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend ``claude`` is on PATH at a version the installer accepts.

    Tests that exercise the install/uninstall flow rely on
    :meth:`ClaudeInstaller.detect` returning ``(True, ...)``, which in turn
    shells out to ``claude --version``. We short-circuit both the lookup and
    the version probe so the suite is hermetic.
    """
    from semantic_version import Version

    monkeypatch.setattr(
        "aiguard.claude.installer.detect_executable", lambda _: Path("/usr/bin/claude")
    )
    monkeypatch.setattr(
        ClaudeInstaller, "_claude_version", staticmethod(lambda _: Version("9.9.9"))
    )


@pytest.fixture
def claude_present(tmp_home: Path, claude_detected: None) -> Path:
    settings = tmp_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{}")
    return settings


@pytest.fixture
def staged_binary(tmp_home: Path) -> Path:
    """Pretend the bootstrap installer already laid out the onedir bundle.

    Install runs need the launcher on disk: the hooks shell out to
    ``ai-guard hook ...``, so the install flow refuses to proceed without a
    bundle launcher present.
    """
    paths.bundle_dir().mkdir(parents=True, exist_ok=True)
    paths.bundle_executable().write_text("#!/bin/sh\nexit 0\n")
    paths.bundle_executable().chmod(0o755)

    paths.local_bin_dir().mkdir(parents=True, exist_ok=True)
    if paths.binary_path().exists() or paths.binary_path().is_symlink():
        paths.binary_path().unlink()
    paths.binary_path().symlink_to(paths.bundle_executable())
    return paths.binary_path()


# =============================================================================
# aiguard/paths.py — Claude config-dir override (CLAUDE_CONFIG_DIR)
# =============================================================================


class TestClaudeConfigDir:
    """``paths.claude_config_dir()`` honours ``$CLAUDE_CONFIG_DIR``.

    Claude Code itself uses the same env var to relocate ``~/.claude/`` for
    multi-account setups; the installer must follow so the hook block lands
    next to the active account's ``settings.json``.
    """

    def test_default_is_dot_claude_under_home(self, tmp_home: Path) -> None:
        assert paths.claude_config_dir() == tmp_home / ".claude"
        assert paths.claude_settings_path() == tmp_home / ".claude" / "settings.json"

    def test_env_var_overrides_default(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        override = tmp_home / "work-claude"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(override))

        assert paths.claude_config_dir() == override
        assert paths.claude_settings_path() == override / "settings.json"

    def test_env_var_expands_tilde(self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/personal-claude")
        assert paths.claude_config_dir() == tmp_home / "personal-claude"

    def test_falls_back_to_persisted_config(self, tmp_home: Path) -> None:
        """A later install/uninstall from a shell that didn't re-export the
        override must still resolve it from ``config.env``, not ``~/.claude``."""
        override = tmp_home / "stored-claude"
        save_config({"CLAUDE_CONFIG_DIR": str(override)})

        assert paths.claude_config_dir() == override
        assert paths.claude_settings_path() == override / "settings.json"

    def test_live_env_wins_over_persisted_config(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_config({"CLAUDE_CONFIG_DIR": str(tmp_home / "stored-claude")})
        live = tmp_home / "live-claude"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(live))

        assert paths.claude_config_dir() == live

    def test_install_writes_to_overridden_settings_path(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        override = tmp_home / "alt-claude"
        override.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(override))

        updated = ClaudeInstaller().install()

        assert updated == [override / "settings.json"]
        data = _read(override / "settings.json")
        assert set(data["hooks"].keys()) == set(HOOK_EVENTS)
        # The default ~/.claude/settings.json must stay untouched.
        assert not (tmp_home / ".claude" / "settings.json").exists()


class TestConfigHome:
    """``paths.config_home()`` honours ``$XDG_CONFIG_HOME``."""

    def test_default_is_dot_config_under_home(self, tmp_home: Path) -> None:
        assert paths.config_home() == tmp_home / ".config"

    def test_env_var_expands_tilde(self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Shell profiles routinely set XDG_CONFIG_HOME=~/.config; a literal ``~``
        # must not resolve relative to the cwd.
        monkeypatch.setenv("XDG_CONFIG_HOME", "~/xdg-config")
        assert paths.config_home() == tmp_home / "xdg-config"


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
        updated = ClaudeInstaller().install()

        assert updated == [settings_path]
        data = _read(settings_path)
        assert set(data["hooks"].keys()) == set(HOOK_EVENTS)
        # In-process hooks: no proxy redirect is written.
        assert "env" not in data

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

        ClaudeInstaller().install()
        data = _read(settings)

        assert data["permissions"] == {"allow": ["Bash"]}
        assert data["model"] == "claude-sonnet-4-6"
        assert "ANTHROPIC_BASE_URL" not in data.get("env", {})

    def test_install_leaves_users_own_base_url_untouched(self, tmp_home: Path) -> None:
        """A user's own ANTHROPIC_BASE_URL (not our proxy) must survive install."""
        settings = _make_claude_dir(tmp_home) / "settings.json"
        settings.write_text(
            json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://upstream.example/v1"}})
        )

        ClaudeInstaller().install()
        data = _read(settings)
        assert data["env"]["ANTHROPIC_BASE_URL"] == "https://upstream.example/v1"

    def test_install_strips_legacy_proxy_redirect(self, tmp_home: Path) -> None:
        """A redirect left by an older proxy-based install is removed on install."""
        settings = _make_claude_dir(tmp_home) / "settings.json"
        settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": PROXY}}))

        ClaudeInstaller().install()
        data = _read(settings)
        assert "ANTHROPIC_BASE_URL" not in data.get("env", {})
        assert set(data["hooks"].keys()) == set(HOOK_EVENTS)

    def test_install_strips_legacy_custom_proxy_redirect(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Upgrade from a proxy install with a NON-default host/port.

        ``config.env`` no longer carries the proxy host/port (and ``install``
        rewrites it before the redirect is stripped), so the only place the
        custom value survives is the environment — which the CLI loads from the
        old ``config.env`` at startup. ``_configured_proxy_url`` must read it
        from there, else the stale redirect (pointing at the dead custom proxy)
        is left behind.
        """
        monkeypatch.setenv("DD_AI_GUARD_PROXY_HOST", "0.0.0.0")
        monkeypatch.setenv("DD_AI_GUARD_PROXY_PORT", "41234")
        settings = _make_claude_dir(tmp_home) / "settings.json"
        settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://0.0.0.0:41234"}}))

        ClaudeInstaller().install()
        data = _read(settings)
        assert "ANTHROPIC_BASE_URL" not in data.get("env", {})

    def test_reinstall_is_idempotent(self, tmp_home: Path) -> None:
        _make_claude_dir(tmp_home)
        ClaudeInstaller().install()
        ClaudeInstaller().install()

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

        ClaudeInstaller().install()
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
        agent.install()
        agent.uninstall()

        data = _read(settings)
        assert data == {
            "hooks": {"PreToolUse": [user_hook]},
            "model": "claude-sonnet-4-6",
        }

    def test_uninstall_restores_chained_upstream_from_config(self, tmp_home: Path) -> None:
        """A legacy proxy redirect is replaced by the upstream captured in config.

        Older installs saved the user's pre-existing upstream into ``config.env``
        as ``DD_AI_GUARD_ANTHROPIC_UPSTREAM``; uninstall restores it when it
        strips the proxy redirect.
        """
        settings = _make_claude_dir(tmp_home) / "settings.json"
        settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": PROXY}}))
        save_config({"DD_AI_GUARD_ANTHROPIC_UPSTREAM": "https://upstream.example/v1"})

        ClaudeInstaller().uninstall()

        data = _read(settings)
        assert data == {"env": {"ANTHROPIC_BASE_URL": "https://upstream.example/v1"}}

    def test_uninstall_removes_legacy_redirect_when_no_original(self, tmp_home: Path) -> None:
        settings = _make_claude_dir(tmp_home) / "settings.json"
        settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": PROXY}}))

        ClaudeInstaller().uninstall()

        data = _read(paths.claude_settings_path())
        # Synthesised env block with only our redirect → gone entirely.
        assert "env" not in data

    def test_is_installed_true_when_hooks_reference_ai_guard(self, tmp_home: Path) -> None:
        _make_claude_dir(tmp_home)
        ClaudeInstaller().install()

        assert ClaudeInstaller().is_installed() is True

    def test_is_installed_false_on_pristine_settings(self, tmp_home: Path) -> None:
        settings = _make_claude_dir(tmp_home) / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "permissions": {"allow": ["Bash"]},
                    "env": {"ANTHROPIC_BASE_URL": "https://upstream.example/v1"},
                }
            )
        )

        assert ClaudeInstaller().is_installed() is False

    def test_is_installed_true_when_only_proxy_env_remains(self, tmp_home: Path) -> None:
        """User-edited settings can leave ``env.ANTHROPIC_BASE_URL`` pointing
        at the proxy while the hook block was manually deleted. ``is_installed``
        must still report True so the top-level uninstall driver runs
        ``ClaudeInstaller.uninstall`` and restores or drops the env entry —
        otherwise we'd delete the binary while Claude kept routing API traffic
        at the now-dead local proxy."""
        settings = _make_claude_dir(tmp_home) / "settings.json"
        settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": PROXY}}))

        assert ClaudeInstaller().is_installed() is True

    def test_detect_finds_executable_on_path(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from semantic_version import Version

        monkeypatch.setattr(
            "aiguard.claude.installer.detect_executable", lambda _: Path("/usr/bin/claude")
        )
        monkeypatch.setattr(
            ClaudeInstaller, "_claude_version", staticmethod(lambda _: Version("9.9.9"))
        )
        supported, message = ClaudeInstaller().detect()
        assert supported is True
        assert "9.9.9" in message
        assert "/usr/bin/claude" in message

    def test_detect_missing_dir_returns_false(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("aiguard.claude.installer.detect_executable", lambda _: None)
        supported, message = ClaudeInstaller().detect()
        assert supported is False
        assert message == "Claude not found"

    def test_detect_rejects_old_version(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from semantic_version import Version

        monkeypatch.setattr(
            "aiguard.claude.installer.detect_executable", lambda _: Path("/usr/bin/claude")
        )
        monkeypatch.setattr(
            ClaudeInstaller, "_claude_version", staticmethod(lambda _: Version("2.0.0"))
        )
        supported, message = ClaudeInstaller().detect()
        assert supported is False
        assert "too old" in message
        assert "2.0.0" in message

    def test_detect_accepts_when_version_unknown(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If we can't parse a version, fall open: the binary is on PATH so
        we report Claude as detected and let the user proceed. The message
        must not interpolate ``None`` into a fake version suffix."""
        monkeypatch.setattr(
            "aiguard.claude.installer.detect_executable", lambda _: Path("/usr/bin/claude")
        )
        monkeypatch.setattr(ClaudeInstaller, "_claude_version", staticmethod(lambda _: None))
        supported, message = ClaudeInstaller().detect()
        assert supported is True
        assert "/usr/bin/claude" in message
        assert "None" not in message


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

    def test_claude_config_dir_passthrough_omitted_when_unset(self, tmp_home: Path) -> None:
        # ClaudeInstaller's only env field is the CLAUDE_CONFIG_DIR passthrough,
        # which is saved only when the user actually set it.
        values = _run_prompt(
            {
                "advanced": True,
                "non_interactive": True,
                "env": {"DD_API_KEY": "k", "DD_APP_KEY": "a", "DD_SERVICE": "s"},
                "agents": [ClaudeInstaller()],
            }
        )
        assert "CLAUDE_CONFIG_DIR" not in values

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

    def test_passthrough_field_omitted_when_env_unset(self) -> None:
        """Tier-PASSTHROUGH fields with no env value and no default must not raise
        and must not land in the resulting config map — they only get persisted
        when the user explicitly sets them."""
        values = _run_prompt(
            {
                "advanced": False,
                "non_interactive": True,
                "env": {"DD_API_KEY": "k", "DD_APP_KEY": "a", "DD_SERVICE": "s"},
                "agents": [ClaudeInstaller()],
            }
        )
        assert "CLAUDE_CONFIG_DIR" not in values

    def test_passthrough_field_persisted_when_env_set(self) -> None:
        """When the user installs with ``CLAUDE_CONFIG_DIR=...``, that value must
        survive into ``config.env`` so the service wrapper re-exports it on every
        proxy restart. Without it the proxy falls back to ``~/.claude`` for
        user-level skills/plugins lookup."""
        values = _run_prompt(
            {
                "advanced": False,
                "non_interactive": True,
                "env": {
                    "DD_API_KEY": "k",
                    "DD_APP_KEY": "a",
                    "DD_SERVICE": "s",
                    "CLAUDE_CONFIG_DIR": "/work/claude",
                },
                "agents": [ClaudeInstaller()],
            }
        )
        assert values["CLAUDE_CONFIG_DIR"] == "/work/claude"

    def test_env_value_wins_over_field_default_on_reinstall(self) -> None:
        """A previously stored / exported value must take precedence over a
        field's hardcoded default when the user just hits Enter at the prompt.

        Tier-1 fields like DD_SITE and DD_ENV ship with defaults; reinstalling
        with the env populated from config.env should reuse those, not silently
        revert to the hardcoded ones."""
        values = _run_prompt(
            {
                "advanced": False,
                "non_interactive": False,
                "env": {
                    "DD_SITE": "datadoghq.eu",
                    "DD_API_KEY": "k",
                    "DD_APP_KEY": "a",
                    "DD_SERVICE": "svc",
                    "DD_ENV": "staging",
                },
            },
            # site, api, app, env, service, version — empty lines accept defaults
            env_inputs="\n\n\n\n\n\n",
        )
        assert values["DD_SITE"] == "datadoghq.eu"
        assert values["DD_ENV"] == "staging"


class TestUiSecretEntry:
    """``aiguard.installer.ui.prompt`` (password=True) — TTY-vs-pipe routing."""

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

        assert ui.prompt("DD_API_KEY", None, password=True) == "from-pwinput"
        assert calls == [{"prompt": "DD_API_KEY: ", "mask": "*"}]

    def test_pwinput_shows_masked_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A stored value is shown as a masked ``[****wxyz]`` hint, and an
        empty pwinput response keeps the original (unmasked) value."""
        monkeypatch.setattr(ui.sys.stdin, "isatty", lambda: True)
        calls: list[dict] = []

        def fake_pwinput(prompt: str, mask: str = "*") -> str:
            calls.append({"prompt": prompt, "mask": mask})
            return ""  # user pressed Enter

        monkeypatch.setattr("pwinput.pwinput", fake_pwinput)

        assert ui.prompt("DD_API_KEY", "abcdefghwxyz", password=True) == "abcdefghwxyz"
        assert calls == [{"prompt": "DD_API_KEY [********wxyz]: ", "mask": "*"}]

    def test_falls_back_to_click_off_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Off-TTY (tests, CI piped stdin) must not call pwinput, which would
        raise ``termios.error`` trying to put a non-tty into raw mode."""
        monkeypatch.setattr(ui.sys.stdin, "isatty", lambda: False)

        def explode(*a, **kw):  # pragma: no cover - should never be called
            raise AssertionError("pwinput must not be called off-TTY")

        monkeypatch.setattr("pwinput.pwinput", explode)
        monkeypatch.setattr(ui.click, "prompt", lambda *a, **kw: "from-click")

        assert ui.prompt("DD_API_KEY", None, password=True) == "from-click"


# =============================================================================
# aiguard/installer/service/ — wrapper, templates, launchd/systemd, log access
# =============================================================================


class TestService:
    """Only teardown remains: ai-guard no longer installs a proxy service, but
    must still tear down one left behind by an older install."""

    def test_wrapper_remove_is_safe_when_missing(self, tmp_home: Path) -> None:
        wrapper.remove()  # nothing to remove yet; must not raise

    def test_wrapper_remove_deletes_existing(self, tmp_home: Path) -> None:
        target = paths.wrapper_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/bin/sh\n")

        wrapper.remove()

        assert not target.exists()

    def test_systemd_uninstall_stops_disables_and_removes_units(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiguard.installer.service import systemd_user

        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(systemd_user.subprocess, "run", fake_run)

        # An older install left units behind.
        paths.systemd_unit_path().parent.mkdir(parents=True, exist_ok=True)
        paths.systemd_unit_path().write_text("[Service]\n")
        paths.systemd_socket_path().write_text("[Socket]\n")

        systemd_user.uninstall()

        assert not paths.systemd_unit_path().exists()
        assert not paths.systemd_socket_path().exists()
        assert ["systemctl", "--user", "disable", "--now", "ai-guard.socket"] in calls

    def test_systemd_uninstall_is_safe_when_units_missing(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiguard.installer.service import systemd_user

        monkeypatch.setattr(
            systemd_user.subprocess,
            "run",
            lambda *a, **kw: MagicMock(returncode=1, stdout="", stderr=""),
        )
        systemd_user.uninstall()  # no units on disk; must not raise

    def test_launchd_uninstall_boots_out_and_removes_plist(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiguard.installer.service import launchd

        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(launchd.subprocess, "run", fake_run)
        monkeypatch.setattr(launchd.os, "getuid", lambda: 501)

        paths.launchd_plist_path().parent.mkdir(parents=True, exist_ok=True)
        paths.launchd_plist_path().write_text("<plist/>")

        launchd.uninstall()

        assert not paths.launchd_plist_path().exists()
        assert any(c[:2] == ["launchctl", "bootout"] for c in calls)

    def test_manager_uninstall_clears_backend_and_wrapper(
        self, tmp_home: Path, stub_platform: None
    ) -> None:
        """``service_manager.uninstall`` removes the backend units and wrapper."""
        from aiguard.installer.service import manager as service_manager

        paths.wrapper_path().parent.mkdir(parents=True, exist_ok=True)
        paths.wrapper_path().write_text("#!/bin/sh\n")
        paths.systemd_unit_path().parent.mkdir(parents=True, exist_ok=True)
        paths.systemd_unit_path().write_text("[Service]\n")

        service_manager.uninstall()

        assert not paths.wrapper_path().exists()
        assert not paths.systemd_unit_path().exists()


# =============================================================================
# aiguard/installer/installer.py — end-to-end `install` / `uninstall` CLI
# =============================================================================


class TestCli:
    def test_install_non_interactive_happy_path(
        self,
        tmp_home: Path,
        stub_platform: None,
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

        # Claude settings carry our hooks but no proxy redirect (in-process hooks).
        data = json.loads(claude_present.read_text())
        assert "PreToolUse" in data["hooks"]
        assert "ANTHROPIC_BASE_URL" not in data.get("env", {})

        # No proxy service is registered any more.
        assert not paths.systemd_unit_path().exists()
        assert not paths.wrapper_path().exists()
        # The launcher binary is still placed so the hook command resolves.
        assert paths.binary_path().exists()

    def test_install_stores_secrets_in_keychain_when_available(
        self,
        tmp_home: Path,
        stub_platform: None,
        claude_present: Path,
        staged_binary: Path,
        fake_keychain: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With a keychain present, DD_API_KEY / DD_APP_KEY go to the keychain
        and never touch config.env; the rest of the config still lands in the file."""
        monkeypatch.setenv("DD_API_KEY", "kc-api")
        monkeypatch.setenv("DD_APP_KEY", "kc-app")
        monkeypatch.setenv("DD_SERVICE", "test-service")

        result = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert result.exit_code == 0, result.output + str(result.exception or "")

        assert fake_keychain == {"DD_API_KEY": "kc-api", "DD_APP_KEY": "kc-app"}

        content = paths.config_env_path().read_text()
        assert "DD_API_KEY" not in content
        assert "DD_APP_KEY" not in content
        # Non-secret config still persisted to the file.
        assert "DD_SERVICE=test-service" in content

    def test_install_migrates_secrets_from_config_to_keychain(
        self,
        tmp_home: Path,
        stub_platform: None,
        claude_present: Path,
        staged_binary: Path,
        fake_keychain: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A pre-existing config.env with secrets (an upgrade from the
        file-only world) must have them lifted into the keychain and stripped
        from the file on the next install."""
        from aiguard.storage import load_config, save_config

        save_config({"DD_API_KEY": "old-api", "DD_APP_KEY": "old-app", "DD_SERVICE": "svc"})
        # No DD_* exported. The CLI loads config.env into the environment before
        # dispatching install; replay that here (via monkeypatch so it's restored).
        for key in ("DD_API_KEY", "DD_APP_KEY", "DD_SERVICE"):
            monkeypatch.delenv(key, raising=False)
        for key, value in load_config().items():
            monkeypatch.setenv(key, value)

        result = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert result.exit_code == 0, result.output + str(result.exception or "")

        assert fake_keychain == {"DD_API_KEY": "old-api", "DD_APP_KEY": "old-app"}
        content = paths.config_env_path().read_text()
        assert "DD_API_KEY" not in content
        assert "DD_APP_KEY" not in content

    def test_reinstall_reuses_keychain_secrets_without_env(
        self,
        tmp_home: Path,
        stub_platform: None,
        claude_present: Path,
        staged_binary: Path,
        fake_keychain: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Re-running --non-interactive install with no env vars must succeed by
        reading the secrets back out of the keychain (they're not in config.env)."""
        from aiguard import keychain

        monkeypatch.setenv("DD_API_KEY", "kc-api")
        monkeypatch.setenv("DD_APP_KEY", "kc-app")
        monkeypatch.setenv("DD_SERVICE", "svc")
        first = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert first.exit_code == 0, first.output + str(first.exception or "")

        monkeypatch.delenv("DD_API_KEY")
        monkeypatch.delenv("DD_APP_KEY")
        # The CLI loads keychain secrets into the environment before install.
        keychain.load_into_env()
        second = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert second.exit_code == 0, second.output + str(second.exception or "")
        assert fake_keychain == {"DD_API_KEY": "kc-api", "DD_APP_KEY": "kc-app"}

    def test_uninstall_deletes_keychain_secrets(
        self,
        tmp_home: Path,
        stub_platform: None,
        claude_present: Path,
        staged_binary: Path,
        fake_keychain: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DD_API_KEY", "kc-api")
        monkeypatch.setenv("DD_APP_KEY", "kc-app")
        monkeypatch.setenv("DD_SERVICE", "svc")
        inst = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert inst.exit_code == 0, inst.output + str(inst.exception or "")
        assert fake_keychain  # secrets are in the keychain

        uninst = CliRunner().invoke(uninstall, ["--yes", "--no-color"])
        assert uninst.exit_code == 0, uninst.output + str(uninst.exception or "")
        assert fake_keychain == {}

    def test_install_leaves_users_own_base_url(
        self,
        tmp_home: Path,
        stub_platform: None,
        staged_binary: Path,
        claude_detected: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A user's own ANTHROPIC_BASE_URL must survive install untouched."""
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

        data = json.loads(settings.read_text())
        assert data["env"]["ANTHROPIC_BASE_URL"] == "https://upstream.example/v1"

    def test_install_persists_claude_config_dir(
        self,
        tmp_home: Path,
        stub_platform: None,
        claude_detected: None,
        staged_binary: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``CLAUDE_CONFIG_DIR`` set at install time must land in ``config.env``.

        The proxy service runs detached from the install shell, so without
        persistence the wrapper sources ``config.env``, finds no override, and
        falls back to ``~/.claude`` when resolving user-level skills/plugins —
        defeating the override for everything except hook writes.
        """
        override = tmp_home / "alt-claude"
        override.mkdir()
        # Stage settings.json under the override so the installer has a file to
        # merge hooks into; ``claude_detected`` already short-circuits the
        # binary lookup and version probe.
        (override / "settings.json").write_text("{}")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(override))
        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.setenv("DD_APP_KEY", "a")
        monkeypatch.setenv("DD_SERVICE", "ai-guard")

        result = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert result.exit_code == 0, result.output + str(result.exception or "")

        # Round-trip through load_config so we don't depend on shlex's quoting
        # rules (paths with no metacharacters survive unquoted; tmp dirs vary).
        from aiguard.storage import load_config

        assert load_config().get("CLAUDE_CONFIG_DIR") == str(override)

    def test_install_omits_claude_config_dir_when_unset(
        self,
        tmp_home: Path,
        stub_platform: None,
        claude_present: Path,
        staged_binary: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without ``CLAUDE_CONFIG_DIR`` in the env, nothing should be written
        for it — leaving ``paths.claude_config_dir()`` free to keep falling
        back to ``~/.claude`` at runtime."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.setenv("DD_APP_KEY", "a")
        monkeypatch.setenv("DD_SERVICE", "ai-guard")

        result = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert result.exit_code == 0, result.output + str(result.exception or "")

        assert "CLAUDE_CONFIG_DIR" not in paths.config_env_path().read_text()

    def test_uninstall_cleans_hooks_when_agent_no_longer_supported(
        self,
        tmp_home: Path,
        stub_platform: None,
        claude_present: Path,
        staged_binary: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Once hooks are wired into settings.json, uninstall must roll them
        back even if the agent itself has been removed or downgraded below the
        supported version between install and uninstall — otherwise the
        binary at ~/.local/bin/ai-guard gets deleted while settings.json keeps
        a dead hook block pointing at it."""
        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.setenv("DD_APP_KEY", "a")
        monkeypatch.setenv("DD_SERVICE", "ai-guard")

        inst = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert inst.exit_code == 0, inst.output + str(inst.exception or "")
        assert json.loads(claude_present.read_text()).get("hooks")

        # Simulate "claude was removed (or downgraded below the min version)
        # after install" — detect() now returns (False, ...) but the hooks
        # block in settings.json still points at ai-guard.
        monkeypatch.setattr("aiguard.claude.installer.detect_executable", lambda _: None)

        uninst = CliRunner().invoke(uninstall, ["--yes", "--no-color"])
        assert uninst.exit_code == 0, uninst.output + str(uninst.exception or "")

        data = json.loads(claude_present.read_text())
        assert not data.get("hooks")

    def test_install_aborts_when_no_agents_pass_detection(
        self,
        tmp_home: Path,
        stub_platform: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Detection that turns up only unsupported agents must abort rather
        than producing a "successful" install with no hooks wired (Claude
        below ``CLAUDE_MIN_VERSION`` is the practical trigger now that the
        version gate lives in ``ClaudeInstaller.detect``)."""
        monkeypatch.setattr("aiguard.claude.installer.detect_executable", lambda _: None)
        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.setenv("DD_APP_KEY", "a")
        monkeypatch.setenv("DD_SERVICE", "ai-guard")

        result = CliRunner().invoke(install, ["--non-interactive", "--no-color"])

        assert result.exit_code != 0
        # No side effects past detection: config + service must not exist.
        assert not paths.config_env_path().exists()
        assert not paths.systemd_unit_path().exists()

    def test_uninstall_leaves_only_app_log(
        self,
        tmp_home: Path,
        stub_platform: None,
        claude_present: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After uninstall, only the proxy's rotating app log survives on disk.

        The wrapper ``exec``s the proxy with no extra log routing, so the
        rotating app log at ``$XDG_STATE_HOME/ai-guard/ai-guard.log`` is the only on-disk
        surface either platform leaves behind.
        """
        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.setenv("DD_APP_KEY", "a")
        monkeypatch.setenv("DD_SERVICE", "ai-guard")

        # Drop a fake onedir bundle + symlink + log so we can check what survives.
        paths.bundle_dir().mkdir(parents=True, exist_ok=True)
        paths.bundle_executable().write_text("#!/bin/sh\nexit 0\n")
        paths.bundle_executable().chmod(0o755)
        paths.local_bin_dir().mkdir(parents=True, exist_ok=True)
        paths.binary_path().symlink_to(paths.bundle_executable())

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
        assert not paths.binary_path().is_symlink()
        assert not paths.bundle_dir().exists()
        # Hooks are gone from the settings file.
        data = json.loads(claude_present.read_text())
        assert "hooks" not in data or data["hooks"] == {}

    def test_reinstall_reuses_stored_config_without_env(
        self,
        tmp_home: Path,
        stub_platform: None,
        claude_present: Path,
        staged_binary: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Re-running --non-interactive install with no env vars must succeed.

        Stored config.env is the source of truth on re-run; the user should not
        have to re-export their secrets every time they re-install. The CLI
        loads config.env into the environment before the command runs, which we
        simulate here with ``storage.load_into_environ()``.
        """
        from aiguard import storage

        monkeypatch.setenv("DD_API_KEY", "first-api")
        monkeypatch.setenv("DD_APP_KEY", "first-app")
        monkeypatch.setenv("DD_SERVICE", "first-service")
        first = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert first.exit_code == 0, first.output + str(first.exception or "")

        monkeypatch.delenv("DD_API_KEY")
        monkeypatch.delenv("DD_APP_KEY")
        # The CLI loads config.env into the environment before dispatching the
        # command; replay that here (via monkeypatch so it's restored on teardown).
        for key, value in storage.load_config().items():
            monkeypatch.setenv(key, value)
        second = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert second.exit_code == 0, second.output + str(second.exception or "")

        content = paths.config_env_path().read_text()
        assert "DD_API_KEY=first-api" in content
        assert "DD_APP_KEY=first-app" in content

    def test_reinstall_env_var_overrides_stored_config(
        self,
        tmp_home: Path,
        stub_platform: None,
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

    def test_install_missing_secret_exits_non_zero(
        self,
        tmp_home: Path,
        stub_platform: None,
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
        claude_present: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`uv run ai-guard install` without a built bundle must exit non-zero
        with a clear error pointing at the bootstrap script — NOT silently write
        a wrapper that exec's a nonexistent path."""
        import sys as _sys

        monkeypatch.setattr(_sys, "frozen", False, raising=False)
        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.setenv("DD_APP_KEY", "a")
        monkeypatch.setenv("DD_SERVICE", "ai-guard")
        # No bundle at paths.bundle_dir() — that's the whole point of the test.
        assert not paths.bundle_executable().exists()

        result = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert result.exit_code == 1
        assert "no AI Guard bundle" in result.output
        assert "pyinstaller" in result.output
        # The wrapper must not have been left behind pointing at a dangling path.
        assert not paths.wrapper_path().exists()

    def test_install_frozen_bundle_copies_itself_to_target(
        self,
        tmp_home: Path,
        stub_platform: None,
        claude_present: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A frozen ai-guard bundle running from anywhere else should copy itself
        into place so the generated wrapper has something to exec."""
        import sys as _sys

        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.setenv("DD_APP_KEY", "a")
        monkeypatch.setenv("DD_SERVICE", "ai-guard")
        assert not paths.binary_path().exists()
        assert not paths.bundle_dir().exists()

        # Pretend we're running as a frozen onedir bundle at a different location.
        elsewhere = tmp_home / "downloads" / "ai-guard"
        elsewhere.mkdir(parents=True, exist_ok=True)
        elsewhere_launcher = elsewhere / "ai-guard"
        elsewhere_launcher.write_text("#!/bin/sh\necho not actually executed\n")
        elsewhere_launcher.chmod(0o755)
        (elsewhere / "_internal").mkdir()
        (elsewhere / "_internal" / "data.bin").write_text("pretend bundled module")

        monkeypatch.setattr(_sys, "frozen", True, raising=False)
        monkeypatch.setattr(_sys, "executable", str(elsewhere_launcher))

        result = CliRunner().invoke(install, ["--non-interactive", "--no-color"])
        assert result.exit_code == 0, result.output + str(result.exception or "")

        # The whole bundle is now where the installer expects it.
        assert paths.bundle_executable().exists()
        assert (paths.bundle_dir() / "_internal" / "data.bin").exists()
        # And the user-facing launcher on PATH is a symlink to the bundled exec.
        assert paths.binary_path().is_symlink()
        assert paths.binary_path().resolve() == paths.bundle_executable().resolve()


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

    def detect(self) -> tuple[bool, str]:
        if self._settings_path.parent.exists():
            return True, f"fake agent at {self._settings_path.parent}"
        return False, "fake agent not found"

    def is_installed(self) -> bool:
        if not self._settings_path.exists():
            return False
        return any(
            h.get("command", "").startswith("ai-guard hook")
            for h in self._load().get("hooks") or []
        )

    def env_fields(self) -> list[Field]:
        # FakeAgent demonstrates a non-Anthropic agent contributing its own
        # tier-2 upstream var, sourced from its own on-disk config.
        return [
            Field(
                "DD_AI_GUARD_FAKE_UPSTREAM",
                "Upstream Fake endpoint",
                default=self._load().get(self.UPSTREAM_KEY) or "https://api.fake.example",
                tier=Tier.ADVANCED,
            ),
        ]

    def install(self) -> list[Path]:
        data = self._load()
        data.setdefault("hooks", []).append({"command": "ai-guard hook fake event"})
        self._write(data)
        return [self._settings_path]

    def uninstall(self) -> list[Path]:
        if not self._settings_path.exists():
            return []
        data = self._load()
        data["hooks"] = [
            h for h in data.get("hooks", []) if not h.get("command", "").startswith("ai-guard hook")
        ]
        if not data["hooks"]:
            data.pop("hooks", None)
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
        register_fake_agent: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-Claude agent runs through the full install + uninstall pipeline."""
        # Set up the fake agent's marker dir so `detect()` succeeds, and seed a
        # pre-existing value so we can verify install leaves unrelated keys alone.
        fake_cfg = tmp_home / ".fake-agent" / "config.json"
        fake_cfg.parent.mkdir(parents=True, exist_ok=True)
        fake_cfg.write_text(json.dumps({"FAKE_API_BASE_URL": "https://my-fake.example/v1"}))

        # Pre-stage a fake onedir bundle so the launcher is in place (the hook
        # command needs it on PATH); production installs get this from the
        # bootstrap script.
        paths.bundle_dir().mkdir(parents=True, exist_ok=True)
        paths.bundle_executable().write_text("#!/bin/sh\nexit 0\n")
        paths.bundle_executable().chmod(0o755)
        paths.local_bin_dir().mkdir(parents=True, exist_ok=True)
        paths.binary_path().symlink_to(paths.bundle_executable())

        monkeypatch.setenv("DD_API_KEY", "k")
        monkeypatch.setenv("DD_APP_KEY", "a")
        monkeypatch.setenv("DD_SERVICE", "ai-guard")

        runner = CliRunner()
        result = runner.invoke(
            installer_cli.install,
            ["--non-interactive", "--no-color", "--agent", "fake"],
        )
        assert result.exit_code == 0, result.output + str(result.exception or "")

        # The fake agent contributed its own tier-2 Field via env_fields(), so
        # its var lands in config.env with the value pre-filled as the default.
        env_content = paths.config_env_path().read_text()
        assert "DD_AI_GUARD_FAKE_UPSTREAM=https://my-fake.example/v1" in env_content

        # Hooks landed in the fake agent's config; unrelated keys are untouched.
        data = json.loads(fake_cfg.read_text())
        assert data["FAKE_API_BASE_URL"] == "https://my-fake.example/v1"
        assert data["hooks"] == [{"command": "ai-guard hook fake event"}]

        # Uninstall reverses everything via the same generic plumbing.
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
