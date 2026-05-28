"""Claude Code integration.

Merges the ai-guard hook block into ``~/.claude/settings.json`` and points
``env.ANTHROPIC_BASE_URL`` at the local proxy. Pre-existing
``ANTHROPIC_BASE_URL`` values are reported back so the installer can use them
as the proxy's upstream and restore them on uninstall.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from semantic_version import Version

from aiguard import paths, storage
from aiguard.constants import AIGuardConstants
from aiguard.installer.agent import AgentInstaller, Field
from aiguard.utils import atomic_write, detect_executable

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
    """Return the full ``hooks`` dict ai-guard injects"""
    return {event: [_hook_block(event)] for event in HOOK_EVENTS}


def _is_ai_guard_entry(entry: dict) -> bool:
    """An entry belongs to us if any of its inner ``command`` hooks starts with
    ``ai-guard hook``."""
    for inner in entry.get("hooks", []) or []:
        cmd = inner.get("command", "")
        if isinstance(cmd, str) and cmd.startswith("ai-guard hook"):
            return True
    return False


def _configured_proxy_url() -> str:
    """The proxy URL the installer would currently write into agent settings.

    Reads ``DD_AI_GUARD_PROXY_HOST`` / ``DD_AI_GUARD_PROXY_PORT`` from
    ``config.env`` (falling back to the constants), so callers can compare
    against ``env.ANTHROPIC_BASE_URL`` to decide whether an entry is ours.
    """
    config = storage.load_config()
    host = config.get("DD_AI_GUARD_PROXY_HOST", AIGuardConstants.PROXY_HOST_DEFAULT)
    port = config.get("DD_AI_GUARD_PROXY_PORT", str(AIGuardConstants.PROXY_PORT_DEFAULT))
    return f"http://{host}:{port}"


class ClaudeInstaller(AgentInstaller):
    name = "Claude Code"

    def detect(self) -> tuple[bool, str]:
        executable = detect_executable("claude")
        if not executable:
            return False, "Claude not found"

        version = self._claude_version(executable)
        if version:
            min_version = Version(AIGuardConstants.CLAUDE_MIN_VERSION)
            if version < min_version:
                return (
                    False,
                    f"Claude {version} is too old (need >= {min_version})",
                )

        version_str = f" v{version}" if version else ""
        return True, f"Claude found at {executable}{version_str}"

    def is_installed(self) -> bool:
        settings_path = paths.claude_settings_path()
        if not settings_path.exists():
            return False
        try:
            data = self._load()
        except RuntimeError:
            return False

        # Check if there is any installed hook
        hooks = data.get("hooks")
        if isinstance(hooks, dict) and any(
            isinstance(entry, dict) and _is_ai_guard_entry(entry)
            for entries in hooks.values()
            if isinstance(entries, list)
            for entry in entries
        ):
            return True

        # Check the proxy base url
        env_block = data.get("env")
        if isinstance(env_block, dict):
            base_url = env_block.get("ANTHROPIC_BASE_URL")
            if isinstance(base_url, str) and base_url == _configured_proxy_url():
                return True
        return False

    def env_fields(self) -> list[Field]:
        return [
            Field(
                "DD_AI_GUARD_ANTHROPIC_UPSTREAM",
                "Upstream Anthropic endpoint",
                default=self._detect_upstream() or AIGuardConstants.ANTHROPIC_UPSTREAM_DEFAULT,
                tier=2,
            ),
        ]

    def install(self, proxy_url: str) -> list[Path]:
        original = self._load()

        merged_hooks = dict(original.get("hooks") or {})
        new_hooks = build_hooks_section()
        for event, blocks in new_hooks.items():
            current = list(merged_hooks.get(event) or [])
            # Drop any prior ai-guard entries for this event so re-install is idempotent.
            current = [b for b in current if not _is_ai_guard_entry(b)]
            current.extend(blocks)
            merged_hooks[event] = current

        env_block = dict(original.get("env") or {})
        env_block["ANTHROPIC_BASE_URL"] = proxy_url

        merged = dict(original)
        merged["hooks"] = merged_hooks
        merged["env"] = env_block

        settings_path = paths.claude_settings_path()
        atomic_write(settings_path, lambda fh: json.dump(merged, fh, indent=2))
        return [settings_path]

    def uninstall(self) -> list[Path]:
        settings_path = paths.claude_settings_path()
        if not settings_path.exists():
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

        # Restore the user's pre-existing upstream if one was captured at install
        # time (DD_AI_GUARD_ANTHROPIC_UPSTREAM in config.env); otherwise drop the
        # ANTHROPIC_BASE_URL key we added so the agent goes back to its default.
        env_block = data.get("env")
        if isinstance(env_block, dict) and "ANTHROPIC_BASE_URL" in env_block:
            upstream = storage.load_config().get("DD_AI_GUARD_ANTHROPIC_UPSTREAM", "")
            if upstream and upstream != AIGuardConstants.ANTHROPIC_UPSTREAM_DEFAULT:
                env_block["ANTHROPIC_BASE_URL"] = upstream
            else:
                env_block.pop("ANTHROPIC_BASE_URL", None)
            if not env_block:
                data.pop("env", None)

        atomic_write(settings_path, lambda fh: json.dump(data, fh, indent=2))
        return [settings_path]

    def _load(self) -> dict:
        settings_path = paths.claude_settings_path()
        if not settings_path.exists():
            return {}
        try:
            return json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"could not parse {settings_path}: {exc.msg} at line {exc.lineno}"
            ) from exc

    def _detect_upstream(self) -> str | None:
        try:
            data = self._load()
        except RuntimeError:
            return None
        existing = (data.get("env") or {}).get("ANTHROPIC_BASE_URL")
        return existing or os.getenv("ANTHROPIC_BASE_URL")

    @staticmethod
    def _claude_version(executable: Path) -> Version | None:
        try:
            result = subprocess.run(
                [str(executable), "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        # ``claude --version`` prints ``"<version> (Claude Code)"``; the first
        # whitespace-separated token is what we feed to ``Version``.
        token = (result.stdout or result.stderr).strip().split(maxsplit=1)
        if not token:
            return None
        try:
            return Version(token[0])
        except ValueError:
            return None
