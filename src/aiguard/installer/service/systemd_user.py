"""Linux systemd ``--user`` integration."""

from __future__ import annotations

import subprocess

from aiguard import paths
from aiguard.constants import AIGuardConstants
from aiguard.installer.templates import render


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        check=check,
        capture_output=True,
        text=True,
    )


def install() -> None:
    unit_path = paths.systemd_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(
        render("ai-guard.service.in", WRAPPER=str(paths.wrapper_path())),
        encoding="utf-8",
    )
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", AIGuardConstants.SYSTEMD_UNIT_NAME)


def uninstall() -> None:
    unit_path = paths.systemd_unit_path()
    # Stop + disable best-effort; if the unit doesn't exist that's fine.
    _systemctl("disable", "--now", AIGuardConstants.SYSTEMD_UNIT_NAME, check=False)
    try:
        unit_path.unlink()
    except FileNotFoundError:
        pass
    _systemctl("daemon-reload", check=False)


def is_running() -> bool:
    result = _systemctl("is-active", AIGuardConstants.SYSTEMD_UNIT_NAME, check=False)
    return result.stdout.strip() == "active"


# ── Log access ────────────────────────────────────────────────────────────────

# Service stdout/stderr is captured by systemd into the journal
# (``StandardOutput=journal`` in the unit). ``journalctl --user`` is the
# canonical reader.


def log_hint() -> str:
    """User-facing command for tailing the service log."""
    return f"journalctl --user -u {AIGuardConstants.SYSTEMD_UNIT_NAME}"


def tail_log(lines: int = 50) -> tuple[str, str]:
    """Return ``(title, body)`` for the last ``lines`` journal entries."""
    cmd = [
        "journalctl",
        "--user",
        "-u",
        AIGuardConstants.SYSTEMD_UNIT_NAME,
        "--no-pager",
        "-n",
        str(lines),
    ]
    title = " ".join(cmd)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5.0)
        body = result.stdout or result.stderr or "(empty)"
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.SubprocessError) as exc:
        body = f"(could not read service log: {exc})"
    return title, body
