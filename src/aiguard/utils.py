"""Small cross-cutting helpers shared across the package.

* :func:`atomic_write` — write a file via tempfile + ``os.replace`` so callers
  never observe a partial file.
* :func:`is_macos` / :func:`is_linux` — platform predicates. Centralised here so
  installer, service manager, and tests all read from one place; tests
  monkeypatch these to exercise both backends on either host OS.
* :func:`detect_executable` — :func:`shutil.which` wrapper returning a
  :class:`Path`. Used by agent installers to test whether a tool is on PATH.
* :func:`fetch_endpoint_id` — portable ``<os_user>@<hostname>`` identifier
  used as the ``ai_guard.usr.id`` tag value across all coding-agent handlers.
"""

from __future__ import annotations

import getpass
import logging
import os
import shutil
import socket
import sys
import tempfile
from collections.abc import Callable
from io import TextIOWrapper
from pathlib import Path

logger = logging.getLogger("ai_guard")


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


def detect_executable(name: str) -> Path | None:
    found = shutil.which(name)
    return Path(found) if found else None


def fetch_hostname() -> str | None:
    try:
        return socket.gethostname()
    except OSError:
        logger.debug("fetch_endpoint_id: socket.gethostname() failed", exc_info=True)
        return None


def fetch_user() -> str | None:
    try:
        return getpass.getuser()
    except Exception:
        logger.debug("fetch_endpoint_id: getpass.getuser() failed", exc_info=True)
        return None


def fetch_endpoint_id() -> str:
    """Return ``<os_user>@<hostname>`` for the current process."""
    hostname = fetch_hostname() or "-"
    user = fetch_user() or "-"
    return f"{user}@{hostname}"
