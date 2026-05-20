"""Claude Code integration.

Merges the ai-guard hook block into ``~/.claude/settings.json`` and points
``env.ANTHROPIC_BASE_URL`` at the local proxy. Pre-existing
``ANTHROPIC_BASE_URL`` values are reported back so the installer can use them
as the proxy's upstream and restore them on uninstall.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from aiguard.installer import backup, paths
from aiguard.installer.agent import AgentInstaller, Field, InstallResult, detect_executable

HOOK_EVENTS: tuple[str, ...] = (
    "SessionStart",
    "SessionEnd",
    "SubagentStart",
    "SubagentStop",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
)

HOOK_COMMAND_PREFIX = "ai-guard hook claude"


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
    """Return the full ``hooks`` dict ai-guard injects, identical in shape to
    ``docker/claude/claude-settings.json``."""
    return {event: [_hook_block(event)] for event in HOOK_EVENTS}


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".settings.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _is_ai_guard_entry(entry: dict) -> bool:
    """An entry belongs to us if any of its inner ``command`` hooks starts with
    ``ai-guard hook``."""
    for inner in entry.get("hooks", []) or []:
        cmd = inner.get("command", "")
        if isinstance(cmd, str) and cmd.startswith("ai-guard hook"):
            return True
    return False


class ClaudeInstaller(AgentInstaller):
    name = "claude"

    def __init__(self, settings_path: Path | None = None) -> None:
        self._settings_path = settings_path or paths.claude_settings_path()

    @property
    def settings_path(self) -> Path:
        return self._settings_path

    def detect(self) -> Path | None:
        if self._settings_path.exists():
            return self._settings_path
        if self._settings_path.parent.exists():
            return self._settings_path
        if detect_executable("claude") is not None:
            return self._settings_path
        return None

    def _load(self) -> dict:
        if not self._settings_path.exists():
            return {}
        try:
            return json.loads(self._settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"could not parse {self._settings_path}: {exc.msg} at line {exc.lineno}"
            ) from exc

    def detect_upstream(self) -> str | None:
        if not self._settings_path.exists():
            return None
        try:
            data = self._load()
        except RuntimeError:
            return None
        existing = (data.get("env") or {}).get("ANTHROPIC_BASE_URL")
        return existing or None

    def env_fields(self, detected_upstream: str | None) -> tuple[Field, ...]:
        # Anthropic-specific upstream URL — only relevant when Claude Code is
        # detected; with --advanced the user can override the default (either
        # the chained pre-existing value or the public Anthropic endpoint).
        return (
            Field(
                "DD_AI_GUARD_ANTHROPIC_UPSTREAM",
                "Upstream Anthropic endpoint",
                default=detected_upstream or "https://api.anthropic.com",
                tier=2,
            ),
        )

    def install_hooks(self, proxy_url: str) -> InstallResult:
        existing = self._load()
        original_base_url = (existing.get("env") or {}).get("ANTHROPIC_BASE_URL")
        restore_data: dict[str, str] = {}
        if original_base_url and original_base_url != proxy_url:
            restore_data["ANTHROPIC_BASE_URL"] = original_base_url

        backup_path = backup.snapshot(self.name, self._settings_path)

        merged_hooks = dict(existing.get("hooks") or {})
        new_hooks = build_hooks_section()
        for event, blocks in new_hooks.items():
            current = list(merged_hooks.get(event) or [])
            # Drop any prior ai-guard entries for this event so re-install is idempotent.
            current = [b for b in current if not _is_ai_guard_entry(b)]
            current.extend(blocks)
            merged_hooks[event] = current

        env_block = dict(existing.get("env") or {})
        env_block["ANTHROPIC_BASE_URL"] = proxy_url

        merged = dict(existing)
        merged["hooks"] = merged_hooks
        merged["env"] = env_block

        _atomic_write_json(self._settings_path, merged)

        return InstallResult(
            settings_path=self._settings_path,
            backup_path=backup_path,
            restore_data=restore_data,
        )

    def uninstall_hooks(self, restore_data: dict[str, str]) -> None:
        if not self._settings_path.exists():
            return

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
            else:
                data["hooks"] = hooks

        original_base_url = restore_data.get("ANTHROPIC_BASE_URL")
        env_block = data.get("env")
        if isinstance(env_block, dict) and "ANTHROPIC_BASE_URL" in env_block:
            if original_base_url:
                env_block["ANTHROPIC_BASE_URL"] = original_base_url
            else:
                env_block.pop("ANTHROPIC_BASE_URL", None)
            if not env_block:
                data.pop("env", None)
            else:
                data["env"] = env_block

        _atomic_write_json(self._settings_path, data)
