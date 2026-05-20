"""Per-agent installer plugin interface.

Each supported coding agent ships a subclass that knows how to detect itself,
inject the ai-guard hook block, and reverse the change. The installer layer
above is agnostic to the specifics.

Per-agent state that the installer must hand back at uninstall time (e.g. a
chained upstream URL, an OAuth token path, anything else) travels through the
opaque ``restore_data`` dict on :class:`InstallResult`. The installer
serialises it to ``restore-state.json`` verbatim and passes it back to the
agent's :meth:`uninstall_hooks` later. The base class never inspects it.
"""

from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class InstallResult:
    settings_path: Path
    backup_path: Path | None
    restore_data: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Field:
    """A single configuration value the installer collects and writes to
    ``~/.ai_guard/config.env``.

    Lives here (not in :mod:`aiguard.installer.prompt`) so agent modules can
    return their own ``Field`` instances from :meth:`AgentInstaller.env_fields`
    without :mod:`prompt` having to import them and create a cycle.
    """

    key: str
    label: str
    default: str | None = None
    secret: bool = False
    tier: int = 1


class AgentInstaller(ABC):
    name: str

    @abstractmethod
    def detect(self) -> Path | None:
        """Return the agent's settings path if it looks installed, else None."""

    @abstractmethod
    def install_hooks(self, proxy_url: str) -> InstallResult:
        """Back up the settings file, merge our hooks, point env at the proxy."""

    @abstractmethod
    def uninstall_hooks(self, restore_data: dict[str, str]) -> None:
        """Remove our hooks and restore agent state from ``restore_data``."""

    def detect_upstream(self) -> str | None:
        """Return an existing upstream URL we should chain through, if any.

        Default is no chaining. Agents that talk to an LLM provider whose base
        URL the user may already have customised should override this.
        """
        return None

    def env_fields(self, detected_upstream: str | None) -> tuple[Field, ...]:
        """Return agent-specific config fields to merge into the prompt list.

        These are tier-2 (prompted only with ``--advanced``; otherwise their
        defaults are written to ``config.env`` silently). The installer only
        asks for them when *this* agent is actually detected, so e.g. the
        Anthropic-specific upstream URL stays out of the prompt list for a
        Codex/Cursor-only install. Default is no extra fields.
        """
        return ()


def _which(cmd: str) -> Path | None:
    found = shutil.which(cmd)
    return Path(found) if found else None


def detect_executable(name: str) -> Path | None:
    return _which(name)


def detect_env_var(key: str) -> str | None:
    value = os.environ.get(key)
    return value if value else None
