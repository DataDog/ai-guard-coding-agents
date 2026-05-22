"""Small cross-cutting helpers shared across the package.

* :func:`atomic_write` — write a file via tempfile + ``os.replace`` so callers
  never observe a partial file.
* :func:`is_macos` / :func:`is_linux` — platform predicates. Centralised here so
  installer, service manager, and tests all read from one place; tests
  monkeypatch these to exercise both backends on either host OS.
* :func:`wait_ready` — pure-stdlib equivalent of ``nc -z host port`` in a
  poll loop. Used by the installer to confirm the proxy came up.
* :func:`detect_executable` — :func:`shutil.which` wrapper returning a
  :class:`Path`. Used by agent installers to test whether a tool is on PATH.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
import time
from collections.abc import Callable
from io import TextIOWrapper
from pathlib import Path


def atomic_write(
    path: Path,
    callback: Callable[[TextIOWrapper], object],
    *,
    mode: int | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            callback(fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def wait_ready(host: str, port: int, timeout: float = 5.0, interval: float = 0.1) -> bool:
    """Poll ``host:port`` until it accepts a TCP connection or ``timeout`` elapses.

    Same semantics as ``nc -z host port`` in a 0.1s × N loop — pure stdlib so
    it works on every platform without depending on ``nc``.
    """
    deadline = time.monotonic() + timeout
    while True:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(min(0.5, interval))
        try:
            if sock.connect_ex((host, port)) == 0:
                return True
        finally:
            sock.close()
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def detect_executable(name: str) -> Path | None:
    found = shutil.which(name)
    return Path(found) if found else None
