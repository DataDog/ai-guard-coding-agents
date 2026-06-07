# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Unit tests for ``src/aiguard/storage.py``.

Storage now persists only ``config.env`` (``DD_API_KEY`` etc.) via
:func:`load_config` / :func:`save_config`. Conversation history is no longer
stored here — the Claude handler reconstructs it from Claude Code transcripts.
The file is written with POSIX-shell quoting and locked to user-only perms.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiguard import paths, storage


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

    def test_empty_value_round_trips(self, tmp_home: Path) -> None:
        storage.save_config({"DD_API_KEY": ""})
        assert storage.load_config() == {"DD_API_KEY": ""}

    def test_invalid_key_rejected(self, tmp_home: Path) -> None:
        with pytest.raises(ValueError):
            storage.save_config({"lowercase_key": "x"})

    def test_no_temp_file_left_behind_on_success(self, tmp_home: Path) -> None:
        storage.save_config({"DD_API_KEY": "x"})
        stragglers = list(paths.config_env_path().parent.glob(".config.env.*"))
        assert stragglers == []

    def test_read_missing_returns_empty(self, tmp_home: Path) -> None:
        assert storage.load_config() == {}

    def test_load_ignores_comments_and_blank_lines(self, tmp_home: Path) -> None:
        path = paths.config_env_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# a comment\n\nDD_SITE=datadoghq.com\nnot-an-assignment\n")
        assert storage.load_config() == {"DD_SITE": "datadoghq.com"}
