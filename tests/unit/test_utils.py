"""Unit tests for src/aiguard/utils.py.

Three small surfaces share this module:

* :class:`TestAtomicWrite`   — tempfile + ``os.replace`` so readers never see a
  partial file; optional mode locks the file down for secrets.
* :class:`TestPlatformPredicates` — ``is_macos`` / ``is_linux`` sniff
  ``sys.platform``; tests monkeypatch the platform string to exercise both
  branches on either host OS.
* :class:`TestWaitReady`     — pure-stdlib ``nc -z`` equivalent used by the
  installer to confirm the proxy is listening before it claims success.
"""

from __future__ import annotations

import socket
import stat
import threading
import time
from pathlib import Path

import pytest

from aiguard import utils


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    try:
        return sock.getsockname()[1]
    finally:
        sock.close()


# =============================================================================
# atomic_write
# =============================================================================


class TestAtomicWrite:
    def test_writes_content(self, tmp_path: Path) -> None:
        target = tmp_path / "out.txt"
        utils.atomic_write(target, lambda fh: fh.write("hello"))
        assert target.read_text() == "hello"

    def test_creates_missing_parent(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "dir" / "out.txt"
        utils.atomic_write(target, lambda fh: fh.write("ok"))
        assert target.read_text() == "ok"

    def test_mode_locks_file_when_passed(self, tmp_path: Path) -> None:
        """``mode=0o600`` is what ``storage.save_config`` passes for config.env."""
        target = tmp_path / "secret"
        utils.atomic_write(target, lambda fh: fh.write("k"), mode=0o600)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_callback_failure_leaves_no_tempfile(self, tmp_path: Path) -> None:
        """If the callback raises, the temp file must be cleaned up."""
        target = tmp_path / "out.txt"

        def boom(_fh: object) -> None:
            raise RuntimeError("nope")

        with pytest.raises(RuntimeError):
            utils.atomic_write(target, boom)

        assert not target.exists()
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith("out.txt")]
        assert leftovers == []

    def test_overwrites_atomically(self, tmp_path: Path) -> None:
        """A second write must replace the first; readers never see an empty file."""
        target = tmp_path / "out.txt"
        utils.atomic_write(target, lambda fh: fh.write("first"))
        utils.atomic_write(target, lambda fh: fh.write("second"))
        assert target.read_text() == "second"


# =============================================================================
# is_macos / is_linux
# =============================================================================


class TestPlatformPredicates:
    def test_is_macos_true_on_darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(utils.sys, "platform", "darwin")
        assert utils.is_macos() is True
        assert utils.is_linux() is False

    def test_is_linux_true_on_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(utils.sys, "platform", "linux")
        assert utils.is_linux() is True
        assert utils.is_macos() is False

    def test_is_linux_matches_linux2_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Legacy ``linux2`` (Python 2 era) and ``linux`` both match by prefix."""
        monkeypatch.setattr(utils.sys, "platform", "linux2")
        assert utils.is_linux() is True

    def test_both_false_on_unsupported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(utils.sys, "platform", "win32")
        assert utils.is_macos() is False
        assert utils.is_linux() is False


# =============================================================================
# wait_ready — TCP-poll equivalent of ``nc -z``
# =============================================================================


class TestWaitReady:
    def test_returns_true_when_port_open(self) -> None:
        port = _free_port()
        srv = socket.socket()
        srv.bind(("127.0.0.1", port))
        srv.listen()
        try:
            assert utils.wait_ready("127.0.0.1", port, timeout=1.0)
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
            assert utils.wait_ready("127.0.0.1", port, timeout=2.0, interval=0.05)
        finally:
            t.join(timeout=2.0)

    def test_returns_false_on_timeout(self) -> None:
        port = _free_port()  # nothing listening
        start = time.monotonic()
        assert not utils.wait_ready("127.0.0.1", port, timeout=0.3, interval=0.05)
        assert time.monotonic() - start < 1.0
