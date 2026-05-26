# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Unit tests for helpers in :mod:`aiguard.proxy.server` that are reused across
coding-agent handlers."""

from __future__ import annotations

import pytest

from aiguard.proxy import server as proxy_server
from aiguard.proxy.server import fetch_user_id


class TestFetchUserId:
    """``fetch_user_id`` returns ``<hostname>/<os_user>`` portably."""

    def test_composes_hostname_and_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(proxy_server.socket, "gethostname", lambda: "my-laptop")
        monkeypatch.setattr(proxy_server.getpass, "getuser", lambda: "alice")
        assert fetch_user_id() == "my-laptop/alice"

    def test_falls_back_to_unknown_when_hostname_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise() -> str:
            raise OSError("no host")

        monkeypatch.setattr(proxy_server.socket, "gethostname", _raise)
        monkeypatch.setattr(proxy_server.getpass, "getuser", lambda: "alice")
        assert fetch_user_id() == "unknown/alice"

    def test_falls_back_to_unknown_when_user_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise() -> str:
            raise OSError("no user")

        monkeypatch.setattr(proxy_server.socket, "gethostname", lambda: "my-laptop")
        monkeypatch.setattr(proxy_server.getpass, "getuser", _raise)
        assert fetch_user_id() == "my-laptop/unknown"

    def test_falls_back_when_hostname_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(proxy_server.socket, "gethostname", lambda: "")
        monkeypatch.setattr(proxy_server.getpass, "getuser", lambda: "alice")
        assert fetch_user_id() == "unknown/alice"
