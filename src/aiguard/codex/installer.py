# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Codex CLI integration.

Merges the ai-guard hook block into Codex's ``hooks.json``
(``~/.codex/hooks.json`` by default, or under ``$CODEX_HOME`` when set — see
:func:`aiguard.paths.codex_config_dir`). The hooks run ai-guard in-process, so
there is no proxy or background service to wire.

Note: Codex requires the user to *trust* a non-managed command hook before it
runs (it records the hook against a hash and re-prompts when the hook changes).
The installer writes the hook config, but the user must approve it inside Codex
on the next run — there is no way to pre-trust it from here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from semantic_version import Version

from aiguard import paths
from aiguard.constants import AIGuardConstants
from aiguard.installer.agent import AgentInstaller, Field, Tier
from aiguard.utils import atomic_write, detect_executable

HOOK_EVENTS: tuple[str, ...] = (
    "SessionStart",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
)

HOOK_COMMAND_PREFIX = "ai-guard hook codex"


def _hook_block(event: str) -> dict:
    return {
        "hooks": [
            {
                "type": "command",
                "command": f"{HOOK_COMMAND_PREFIX} {event}",
            }
        ]
    }


def build_hooks_section() -> dict:
    """Return the full ``hooks`` dict ai-guard injects into ``hooks.json``."""
    return {event: [_hook_block(event)] for event in HOOK_EVENTS}


def _is_ai_guard_entry(entry: dict) -> bool:
    """An entry belongs to us if any inner ``command`` starts with ``ai-guard hook``."""
    for inner in entry.get("hooks", []) or []:
        cmd = inner.get("command", "")
        if isinstance(cmd, str) and cmd.startswith("ai-guard hook"):
            return True
    return False


class CodexInstaller(AgentInstaller):
    name = "Codex CLI"

    def detect(self) -> tuple[bool, str]:
        executable = detect_executable("codex")
        if not executable:
            return False, "Codex not found"

        version = self._codex_version(executable)
        if version:
            min_version = Version(AIGuardConstants.CODEX_MIN_VERSION)
            if version < min_version:
                return (
                    False,
                    f"Codex {version} is too old for hooks (need >= {min_version})",
                )

        version_str = f" v{version}" if version else ""
        return True, f"Codex found at {executable}{version_str}"

    def is_installed(self) -> bool:
        hooks_path = paths.codex_hooks_path()
        if not hooks_path.exists():
            return False
        try:
            data = self._load()
        except RuntimeError:
            return False

        hooks = data.get("hooks")
        return isinstance(hooks, dict) and any(
            isinstance(entry, dict) and _is_ai_guard_entry(entry)
            for entries in hooks.values()
            if isinstance(entries, list)
            for entry in entries
        )

    def env_fields(self) -> list[Field]:
        return [
            # The hook honours $CODEX_HOME when locating hooks.json / auth.json,
            # so persist it when the user has it set.
            Field(
                "CODEX_HOME",
                "Codex home directory override",
                default=None,
                tier=Tier.PASSTHROUGH,
            ),
        ]

    def install(self) -> list[Path]:
        original = self._load()

        merged_hooks = dict(original.get("hooks") or {})
        new_hooks = build_hooks_section()
        for event, blocks in new_hooks.items():
            current = list(merged_hooks.get(event) or [])
            # Drop any prior ai-guard entries for this event so re-install is idempotent.
            current = [b for b in current if not (isinstance(b, dict) and _is_ai_guard_entry(b))]
            current.extend(blocks)
            merged_hooks[event] = current

        merged = dict(original)
        merged["hooks"] = merged_hooks

        hooks_path = paths.codex_hooks_path()
        atomic_write(hooks_path, lambda fh: json.dump(merged, fh, indent=2))
        return [hooks_path]

    def uninstall(self) -> list[Path]:
        hooks_path = paths.codex_hooks_path()
        if not hooks_path.exists():
            return []

        data = self._load()
        hooks = data.get("hooks")
        if isinstance(hooks, dict):
            for event in list(hooks.keys()):
                entries = hooks.get(event) or []
                if not isinstance(entries, list):
                    continue
                kept = [e for e in entries if isinstance(e, dict) and not _is_ai_guard_entry(e)]
                if kept:
                    hooks[event] = kept
                else:
                    hooks.pop(event, None)
            if not hooks:
                data.pop("hooks", None)

        atomic_write(hooks_path, lambda fh: json.dump(data, fh, indent=2))
        return [hooks_path]

    def _load(self) -> dict:
        hooks_path = paths.codex_hooks_path()
        if not hooks_path.exists():
            return {}
        try:
            return json.loads(hooks_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"could not parse {hooks_path}: {exc.msg} at line {exc.lineno}"
            ) from exc

    @staticmethod
    def _codex_version(executable: Path) -> Version | None:
        try:
            result = subprocess.run(
                [str(executable), "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        # ``codex --version`` prints ``"codex-cli 0.34.0"``; the version is the
        # last whitespace-separated token.
        tokens = (result.stdout or result.stderr).strip().split()
        if not tokens:
            return None
        try:
            return Version(tokens[-1])
        except ValueError:
            return None
