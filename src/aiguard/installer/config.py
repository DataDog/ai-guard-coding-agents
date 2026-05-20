"""Atomic, 0600 read/write for ``~/.ai_guard/config.env``."""

from __future__ import annotations

import os
import re
import shlex
import tempfile
from pathlib import Path

from aiguard.installer import paths

_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _quote(value: str) -> str:
    """Return the shell-safe representation of ``value`` for ``. config.env``.

    The wrapper script sources this file with ``set -a; . config.env; set +a``,
    so values have to survive POSIX shell parsing.
    """
    if value == "":
        return '""'
    return shlex.quote(value)


def serialize(values: dict[str, str]) -> str:
    lines: list[str] = []
    for key, value in values.items():
        if not _KEY_RE.match(key):
            raise ValueError(f"refusing to write malformed env var name: {key!r}")
        lines.append(f"{key}={_quote(value)}")
    return "\n".join(lines) + "\n"


def parse(text: str) -> dict[str, str]:
    """Tolerant parser, sufficient for files we wrote with :func:`serialize`."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not _KEY_RE.match(key):
            continue
        # Use shlex to undo the quoting applied by serialize().
        parts = shlex.split(value, comments=False, posix=True)
        out[key] = parts[0] if parts else ""
    return out


def read(path: Path | None = None) -> dict[str, str]:
    target = path or paths.config_env_path()
    if not target.exists():
        return {}
    return parse(target.read_text(encoding="utf-8"))


def write(values: dict[str, str], path: Path | None = None) -> None:
    target = path or paths.config_env_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = serialize(values).encode("utf-8")

    # Create the temp file mode 0600 from the start so secrets are never
    # readable by other users between the write and the chmod.
    fd, tmp_name = tempfile.mkstemp(
        prefix=".config.env.",
        dir=str(target.parent),
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        # Best-effort cleanup of the temp file if anything went wrong.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
