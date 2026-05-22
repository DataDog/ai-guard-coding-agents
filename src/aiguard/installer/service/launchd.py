"""macOS launchd (LaunchAgent) integration."""

from __future__ import annotations

import os
import subprocess

from aiguard import paths
from aiguard.constants import AIGuardConstants
from aiguard.installer.templates import render


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["launchctl", *args],
        check=check,
        capture_output=True,
        text=True,
    )


def install() -> None:
    plist_path = paths.launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(
        render(
            "com.datadoghq.ai-guard.plist.in",
            LABEL=AIGuardConstants.LAUNCHD_LABEL,
            WRAPPER=str(paths.wrapper_path()),
            HOME=str(paths.home()),
        ),
        encoding="utf-8",
    )

    # `launchctl bootstrap` is the modern path (macOS 10.10+). It rejects a
    # service that's already loaded, so unload first if needed.
    _launchctl("bootout", f"{_domain()}/{AIGuardConstants.LAUNCHD_LABEL}", check=False)
    result = _launchctl("bootstrap", _domain(), str(plist_path), check=False)
    if result.returncode != 0:
        # Fall back to the legacy syntax (older macOS).
        legacy = _launchctl("load", "-w", str(plist_path), check=False)
        if legacy.returncode != 0:
            raise RuntimeError(
                f"failed to register LaunchAgent: {result.stderr.strip() or legacy.stderr.strip()}"
            )


def uninstall() -> None:
    plist_path = paths.launchd_plist_path()
    _launchctl("bootout", f"{_domain()}/{AIGuardConstants.LAUNCHD_LABEL}", check=False)
    _launchctl("unload", str(plist_path), check=False)
    try:
        plist_path.unlink()
    except FileNotFoundError:
        pass


def is_running() -> bool:
    result = _launchctl("print", f"{_domain()}/{AIGuardConstants.LAUNCHD_LABEL}", check=False)
    return result.returncode == 0


# ── Log access ────────────────────────────────────────────────────────────────

# Service stdout/stderr is piped through ``logger -t ai-guard`` by the wrapper
# (see ai-guard-service.sh.in) so it lands in the macOS unified log. ``log
# show`` is the canonical reader.


def log_hint() -> str:
    """User-facing command for tailing the service log."""
    return 'log show --predicate \'eventMessage CONTAINS "ai-guard"\' --info --last 1h'


def tail_log(lines: int = 50) -> tuple[str, str]:
    """Return ``(title, body)`` for the most-recent service log entries.

    ``lines`` is accepted for API symmetry with the systemd backend but ignored
    here — ``log show`` filters by time window, not row count. We use ``--last
    5m`` to keep the panel digestible when called from a readiness failure.
    """
    cmd = [
        "log",
        "show",
        "--predicate",
        'eventMessage CONTAINS "ai-guard"',
        "--info",
        "--last",
        "5m",
    ]
    title = " ".join(cmd)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5.0)
        body = result.stdout or result.stderr or "(empty)"
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.SubprocessError) as exc:
        body = f"(could not read service log: {exc})"
    return title, body
