# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Binary integration: drive ``ai-guard hook`` through the built executable.

Skipped unless the PyInstaller onedir bundle is present at
``dist/ai-guard/ai-guard[.exe]`` (or at the path in ``AI_GUARD_BINARY``). CI
runs these from the smoke job on every OS/arch after building the bundle.

Hooks run in-process inside the binary, so there is no proxy/server to start.
Without Datadog credentials the AI Guard client can't be constructed; the hook
command swallows that error by design (a failing hook must never break the host
agent), so these tests assert the binary dispatches the command and exits 0
rather than asserting an allow/block verdict.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_LAUNCHER_NAME = "ai-guard.exe" if sys.platform == "win32" else "ai-guard"
BINARY = (
    Path(os.environ["AI_GUARD_BINARY"])
    if os.environ.get("AI_GUARD_BINARY")
    else REPO_ROOT / "dist" / "ai-guard" / _LAUNCHER_NAME
)

pytestmark = pytest.mark.binary

if not BINARY.exists():
    pytest.skip(
        f"ai-guard binary not found at {BINARY} — build it with `pyinstaller ai-guard.spec` "
        "or set AI_GUARD_BINARY to skip this hint",
        allow_module_level=True,
    )


def _run_hook(hook_name: str, event: dict, home: Path) -> subprocess.CompletedProcess[bytes]:
    """Invoke ``ai-guard hook claude <hook_name>`` with ``event`` on stdin.

    Runs under an isolated HOME with no Datadog credentials so nothing reaches
    the network.
    """
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("DD_API_KEY", None)
    env.pop("DD_APP_KEY", None)
    return subprocess.run(
        [str(BINARY), "hook", "claude", hook_name],
        input=json.dumps(event).encode(),
        capture_output=True,
        timeout=60,
        env=env,
    )


def test_binary_dispatches_session_start(tmp_path: Path) -> None:
    proc = _run_hook("SessionStart", {"session_id": "s-bin"}, tmp_path)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert proc.stdout == b""


def test_binary_dispatches_pre_tool_use_with_transcript(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "proj"
    project.mkdir(parents=True)
    transcript = project / "s-bin.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8",
    )

    event = {
        "hook_event_name": "PreToolUse",
        "session_id": "s-bin",
        "transcript_path": str(transcript),
        "tool_name": "Bash",
        "tool_use_id": "tu1",
    }
    proc = _run_hook("PreToolUse", event, tmp_path)

    # No DD credentials → client construction fails and is swallowed; the binary
    # must still exit cleanly without emitting a blocking decision.
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert proc.stdout == b""


def test_binary_tolerates_garbage_payload(tmp_path: Path) -> None:
    proc = subprocess.run(
        [str(BINARY), "hook", "claude", "PreToolUse"],
        input=b"{not json",
        capture_output=True,
        timeout=60,
        env={**os.environ, "HOME": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
