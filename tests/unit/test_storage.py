# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Unit tests for src/aiguard/storage.py.

The public API is intentionally minimal: ``load_messages``, ``save_messages``,
and ``delete_messages``. Append-style accumulation is the caller's
responsibility (load → extend → save).

The ``agent`` and ``session_id`` arguments flow in from request metadata that
the proxy does not control, so ``_session_file`` resolves the candidate path
and rejects anything that escapes the storage root (``DD_AI_GUARD_HOME``,
defaults to ``~/.ai_guard``). Path-traversal hardening is exercised at the
bottom of this file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ddtrace.appsec.ai_guard import ContentPart, Function, Message, ToolCall

from aiguard import paths, storage


def test_load_returns_empty_when_file_missing(tmp_home: Path) -> None:
    assert storage.load_messages("claude", "missing") == []


def test_load_returns_empty_when_session_id_blank(tmp_home: Path) -> None:
    assert storage.load_messages("claude", "") == []
    assert storage.load_messages("", "x") == []


def test_save_then_load_round_trip(tmp_home: Path) -> None:
    msgs: list[Message] = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
    ]
    storage.save_messages("claude", "s1", msgs)
    assert storage.load_messages("claude", "s1") == msgs


def test_save_then_load_preserves_content_parts(tmp_home: Path) -> None:
    msgs: list[Message] = [
        Message(role="system", content="be nice"),
        Message(
            role="user",
            content=[
                ContentPart(type="text", text="look at this"),
                ContentPart(type="image", text='{"source": "..."}'),
            ],
        ),
    ]
    storage.save_messages("claude", "parts", msgs)
    assert storage.load_messages("claude", "parts") == msgs


def test_save_then_load_preserves_tool_calls_and_tool_role(tmp_home: Path) -> None:
    call = ToolCall(
        id="tu1",
        function=Function(name="Bash", arguments='{"command": "ls"}'),
    )
    msgs: list[Message] = [
        Message(role="user", content="run ls"),
        Message(role="assistant", tool_calls=[call]),
        Message(role="tool", tool_call_id="tu1", content="drwxr-xr-x"),
    ]
    storage.save_messages("claude", "tools", msgs)
    assert storage.load_messages("claude", "tools") == msgs


def test_save_overwrites_existing_file(tmp_home: Path) -> None:
    storage.save_messages("claude", "s1", [Message(role="user", content="first")])
    storage.save_messages("claude", "s1", [Message(role="user", content="second")])
    assert storage.load_messages("claude", "s1") == [Message(role="user", content="second")]


def test_load_then_save_supports_append_semantics(tmp_home: Path) -> None:
    """Callers that want append behavior do load → extend → save themselves."""
    storage.save_messages("claude", "s2", [Message(role="user", content="a")])
    full = storage.load_messages("claude", "s2") + [Message(role="assistant", content="b")]
    storage.save_messages("claude", "s2", full)
    assert storage.load_messages("claude", "s2") == [
        Message(role="user", content="a"),
        Message(role="assistant", content="b"),
    ]


def test_save_creates_agent_subdirectory(tmp_home: Path) -> None:
    storage.save_messages("claude", "s3", [Message(role="user", content="x")])
    assert (tmp_home / ".ai_guard" / "claude").is_dir()
    assert (tmp_home / ".ai_guard" / "claude" / "s3.json").is_file()


def test_save_is_no_op_with_empty_session_or_agent(tmp_home: Path) -> None:
    storage.save_messages("", "s4", [Message(role="user", content="x")])
    storage.save_messages("claude", "", [Message(role="user", content="x")])
    assert not (tmp_home / ".ai_guard").exists()


def test_save_is_no_op_with_none_messages(tmp_home: Path) -> None:
    storage.save_messages("claude", "s_none", None)  # type: ignore[arg-type]
    assert not (tmp_home / ".ai_guard").exists()


def test_save_atomic_no_tmp_left_behind(tmp_home: Path) -> None:
    storage.save_messages("claude", "s5", [Message(role="user", content="x")])
    files = list((tmp_home / ".ai_guard" / "claude").iterdir())
    assert [p.name for p in files] == ["s5.json"]


def test_delete_removes_existing_file(tmp_home: Path) -> None:
    storage.save_messages("claude", "s_del", [Message(role="user", content="x")])
    path = tmp_home / ".ai_guard" / "claude" / "s_del.json"
    assert path.is_file()
    storage.delete_messages("claude", "s_del")
    assert not path.exists()
    assert storage.load_messages("claude", "s_del") == []


def test_delete_is_no_op_when_file_missing(tmp_home: Path) -> None:
    storage.delete_messages("claude", "never-existed")  # must not raise


def test_delete_is_no_op_with_empty_session_or_agent(tmp_home: Path) -> None:
    storage.save_messages("claude", "s", [Message(role="user", content="x")])
    storage.delete_messages("", "s")
    storage.delete_messages("claude", "")
    assert (tmp_home / ".ai_guard" / "claude" / "s.json").is_file()


def test_load_tolerates_malformed_file(tmp_home: Path) -> None:
    path = tmp_home / ".ai_guard" / "claude" / "broken.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert storage.load_messages("claude", "broken") == []


def test_load_returns_empty_when_file_is_not_a_list(tmp_home: Path) -> None:
    path = tmp_home / ".ai_guard" / "claude" / "obj.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"unexpected": "shape"}))
    assert storage.load_messages("claude", "obj") == []


def test_save_honors_dd_ai_guard_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DD_AI_GUARD_HOME", str(tmp_path))
    storage.save_messages("claude", "s6", [Message(role="user", content="x")])
    assert (tmp_path / "claude" / "s6.json").is_file()


def test_load_honors_dd_ai_guard_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DD_AI_GUARD_HOME", str(tmp_path))
    path = tmp_path / "claude" / "s7.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([Message(role="user", content="x")]))
    assert storage.load_messages("claude", "s7") == [Message(role="user", content="x")]


def test_default_root_is_home_dot_ai_guard(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DD_AI_GUARD_HOME", raising=False)
    storage.save_messages("claude", "s8", [Message(role="user", content="x")])
    assert (tmp_home / ".ai_guard" / "claude" / "s8.json").is_file()


# ── Path-traversal hardening ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "session_id",
    [
        "../../escape",  # climbs above the storage root
        "../../../../etc/passwd",
        "/etc/passwd",  # absolute path replaces the join
        "with\x00nul",  # NUL byte → ValueError on resolve
    ],
)
def test_save_rejects_session_id_that_escapes_root(tmp_home: Path, session_id: str) -> None:
    storage.save_messages("claude", session_id, [Message(role="user", content="x")])
    json_files = (
        list((tmp_home / ".ai_guard").rglob("*.json")) if (tmp_home / ".ai_guard").exists() else []
    )
    assert json_files == []


@pytest.mark.parametrize(
    "agent",
    [
        "..",
        "../escape",
        "/absolute",
        "with\x00nul",
    ],
)
def test_save_rejects_agent_that_escapes_root(tmp_home: Path, agent: str) -> None:
    storage.save_messages(agent, "s", [Message(role="user", content="x")])
    json_files = (
        list((tmp_home / ".ai_guard").rglob("*.json")) if (tmp_home / ".ai_guard").exists() else []
    )
    assert json_files == []


def test_load_returns_empty_when_path_escapes_root(tmp_home: Path) -> None:
    assert storage.load_messages("claude", "../../escape") == []
    assert storage.load_messages("..", "s") == []


def test_delete_is_noop_when_path_escapes_root(tmp_home: Path) -> None:
    storage.save_messages("claude", "s", [Message(role="user", content="x")])
    storage.delete_messages("claude", "../../escape")  # must not raise nor remove the legit file
    assert storage.load_messages("claude", "s") == [Message(role="user", content="x")]


# ── config.env round-trip ─────────────────────────────────────────────────────


class TestConfig:
    def test_round_trip(self, tmp_home: Path) -> None:
        values = {
            "DD_API_KEY": "abc123",
            "DD_APP_KEY": "def456",
            "DD_SITE": "datadoghq.com",
            "DD_AI_GUARD_BLOCK": "True",
        }
        storage.save_config(values)
        assert storage.load_config() == values

    def test_file_is_mode_0600(self, tmp_home: Path) -> None:
        storage.save_config({"DD_API_KEY": "secret"})
        mode = paths.config_env_path().stat().st_mode & 0o777
        assert mode == 0o600

    def test_values_with_special_chars_round_trip(self, tmp_home: Path) -> None:
        values = {
            "DD_SITE": "datadoghq.com",
            "DD_AI_GUARD_TAG": "value with spaces",
            "DD_AI_GUARD_QUOTE": 'has"double"quotes',
            "DD_AI_GUARD_DOLLAR": "literal $HOME",
        }
        storage.save_config(values)
        assert storage.load_config() == values

    def test_invalid_key_rejected(self, tmp_home: Path) -> None:
        with pytest.raises(ValueError):
            storage.save_config({"lowercase_key": "x"})

    def test_no_temp_file_left_behind_on_success(self, tmp_home: Path) -> None:
        storage.save_config({"DD_API_KEY": "x"})
        stragglers = list(paths.state_dir().glob(".config.env.*"))
        assert stragglers == []

    def test_read_missing_returns_empty(self, tmp_home: Path) -> None:
        assert storage.load_config() == {}
