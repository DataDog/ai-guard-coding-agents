# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Unit tests for the Codex installer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from semantic_version import Version

from aiguard import paths
from aiguard.codex.installer import HOOK_EVENTS, CodexInstaller, build_hooks_section


def _pin_version(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    monkeypatch.setattr(
        "aiguard.codex.installer.detect_executable", lambda name: Path("/usr/bin/codex")
    )
    monkeypatch.setattr(
        CodexInstaller, "_codex_version", staticmethod(lambda exe: Version(version))
    )


def _read_hooks(tmp_home: Path) -> dict:
    return json.loads(paths.codex_hooks_path().read_text(encoding="utf-8"))


class TestBuildHooksSection:
    def test_one_entry_per_event(self) -> None:
        section = build_hooks_section()
        assert set(section) == set(HOOK_EVENTS)
        cmd = section["PreToolUse"][0]["hooks"][0]["command"]
        assert cmd == "ai-guard hook codex PreToolUse"


class TestInstall:
    def test_writes_hooks_json(self, tmp_home: Path) -> None:
        updated = CodexInstaller().install()
        assert updated == [paths.codex_hooks_path()]
        data = _read_hooks(tmp_home)
        assert set(data["hooks"]) == set(HOOK_EVENTS)

    def test_preserves_user_hooks_and_config(self, tmp_home: Path) -> None:
        hooks_path = paths.codex_hooks_path()
        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        hooks_path.write_text(
            json.dumps(
                {
                    "someOtherKey": True,
                    "hooks": {
                        "PreToolUse": [
                            {"hooks": [{"type": "command", "command": "my-own-linter"}]}
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        CodexInstaller().install()
        data = _read_hooks(tmp_home)
        assert data["someOtherKey"] is True
        commands = [h["command"] for e in data["hooks"]["PreToolUse"] for h in e["hooks"]]
        assert "my-own-linter" in commands
        assert "ai-guard hook codex PreToolUse" in commands

    def test_reinstall_is_idempotent(self, tmp_home: Path) -> None:
        CodexInstaller().install()
        CodexInstaller().install()
        data = _read_hooks(tmp_home)
        commands = [h["command"] for e in data["hooks"]["PreToolUse"] for h in e["hooks"]]
        assert commands.count("ai-guard hook codex PreToolUse") == 1

    def test_is_installed_roundtrip(self, tmp_home: Path) -> None:
        installer = CodexInstaller()
        assert installer.is_installed() is False
        installer.install()
        assert installer.is_installed() is True


class TestUninstall:
    def test_removes_only_ai_guard_entries(self, tmp_home: Path) -> None:
        hooks_path = paths.codex_hooks_path()
        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        hooks_path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {"hooks": [{"type": "command", "command": "my-own-linter"}]}
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        CodexInstaller().install()
        CodexInstaller().uninstall()
        data = _read_hooks(tmp_home)
        commands = [h["command"] for e in data["hooks"]["PreToolUse"] for h in e["hooks"]]
        assert commands == ["my-own-linter"]

    def test_uninstall_no_file_is_noop(self, tmp_home: Path) -> None:
        assert CodexInstaller().uninstall() == []


class TestDetect:
    def test_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("aiguard.codex.installer.detect_executable", lambda name: None)
        supported, message = CodexInstaller().detect()
        assert supported is False
        assert "not found" in message

    def test_version_too_old(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _pin_version(monkeypatch, "0.34.0")
        supported, message = CodexInstaller().detect()
        assert supported is False
        assert "too old" in message

    def test_recent_version_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _pin_version(monkeypatch, "0.140.0")
        supported, _ = CodexInstaller().detect()
        assert supported is True
