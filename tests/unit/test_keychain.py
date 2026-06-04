# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Unit tests for ``src/aiguard/keychain.py``.

The OS keychain is stubbed two ways via conftest fixtures:

* ``_no_keychain`` (autouse) makes :func:`keychain._keyring` report no backend,
  so every test starts in the file-fallback world unless it opts in.
* ``fake_keychain`` swaps in an in-memory backend and yields the backing dict,
  so the real ``store``/``load``/``delete`` logic runs end-to-end.
"""

from __future__ import annotations

import pytest

from aiguard import keychain


class TestNoBackend:
    """With no reachable keychain, every operation degrades gracefully."""

    def test_available_is_false(self) -> None:
        assert keychain.available() is False

    def test_store_returns_false(self) -> None:
        assert keychain.store("DD_API_KEY", "secret") is False

    def test_load_returns_none(self) -> None:
        assert keychain.load("DD_API_KEY") is None

    def test_load_secrets_is_empty(self) -> None:
        assert keychain.load_secrets() == {}

    def test_delete_is_noop(self) -> None:
        keychain.delete("DD_API_KEY")  # must not raise

    def test_load_into_env_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DD_API_KEY", raising=False)
        keychain.load_into_env()
        import os

        assert "DD_API_KEY" not in os.environ


class TestWithBackend:
    def test_available_is_true(self, fake_keychain: dict[str, str]) -> None:
        assert keychain.available() is True

    def test_store_then_load_round_trip(self, fake_keychain: dict[str, str]) -> None:
        assert keychain.store("DD_API_KEY", "abc123") is True
        assert keychain.load("DD_API_KEY") == "abc123"
        # Landed under the env-var name in the backing store.
        assert fake_keychain["DD_API_KEY"] == "abc123"

    def test_load_missing_returns_none(self, fake_keychain: dict[str, str]) -> None:
        assert keychain.load("DD_APP_KEY") is None

    def test_delete_removes_entry(self, fake_keychain: dict[str, str]) -> None:
        keychain.store("DD_API_KEY", "abc123")
        keychain.delete("DD_API_KEY")
        assert keychain.load("DD_API_KEY") is None

    def test_delete_missing_is_noop(self, fake_keychain: dict[str, str]) -> None:
        keychain.delete("DD_API_KEY")  # must not raise

    def test_load_secrets_returns_only_present_keys(self, fake_keychain: dict[str, str]) -> None:
        keychain.store("DD_API_KEY", "k")
        assert keychain.load_secrets() == {"DD_API_KEY": "k"}
        keychain.store("DD_APP_KEY", "a")
        assert keychain.load_secrets() == {"DD_API_KEY": "k", "DD_APP_KEY": "a"}

    def test_load_into_env_fills_unset_vars(
        self, fake_keychain: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        monkeypatch.delenv("DD_API_KEY", raising=False)
        keychain.store("DD_API_KEY", "from-keychain")
        keychain.load_into_env()
        assert os.environ["DD_API_KEY"] == "from-keychain"

    def test_load_into_env_does_not_override_existing(
        self, fake_keychain: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        monkeypatch.setenv("DD_API_KEY", "from-env")
        keychain.store("DD_API_KEY", "from-keychain")
        keychain.load_into_env()
        assert os.environ["DD_API_KEY"] == "from-env"
