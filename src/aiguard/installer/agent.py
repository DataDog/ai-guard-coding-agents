"""Per-agent installer plugin interface.

Each supported coding agent ships a subclass that knows how to detect itself,
inject the ai-guard hook block, and reverse the change. The installer layer
above is agnostic to the specifics.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path


class Tier(IntEnum):
    """How the installer treats a :class:`Field` during ``ai-guard install``.

    * :attr:`REQUIRED` — always prompted; in ``--non-interactive`` it must
      come from the environment (or be carried over from ``config.env``).
    * :attr:`ADVANCED` — prompted only under ``--advanced``; otherwise the
      hardcoded default is written silently.
    * :attr:`SILENT` — never prompted; the hardcoded default is always
      written (env overrides still win).
    * :attr:`PASSTHROUGH` — never prompted, no default. Persisted to
      ``config.env`` only when the user actually sets it in the environment
      (or it survives from a prior install). Use for env vars that the
      service must inherit verbatim when present (e.g. ``CLAUDE_CONFIG_DIR``).
    """

    REQUIRED = 1
    ADVANCED = 2
    SILENT = 3
    PASSTHROUGH = 4


@dataclass(frozen=True)
class Field:
    """A single configuration value the installer collects and writes to
    ``$XDG_CONFIG_HOME/ai-guard/config.env``.

    Lives in this lightweight module so agent plugins can declare ``Field``
    instances via :meth:`AgentInstaller.env_fields` without importing the
    installer's UI / CLI stack.
    """

    key: str
    label: str
    default: str | None = None
    secret: bool = False
    tier: Tier = Tier.REQUIRED


class AgentInstaller(ABC):
    name: str

    @abstractmethod
    def detect(self) -> tuple[bool, str]:
        """Return ``(supported, message)``."""

    @abstractmethod
    def is_installed(self) -> bool:
        """Return ``True`` when currently installed for the agent"""

    @abstractmethod
    def install(self) -> list[Path]:
        """Merge the ai-guard hooks into the agent's settings."""

    @abstractmethod
    def uninstall(self) -> list[Path]:
        """Remove the ai-guard hooks from the agent's settings."""

    def env_fields(self) -> list[Field]:
        """Return agent-specific config fields to merge into the prompt list."""
        return []
