"""Per-agent installer plugin interface.

Each supported coding agent ships a subclass that knows how to detect itself,
inject the ai-guard hook block, and reverse the change. The installer layer
above is agnostic to the specifics.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Field:
    """A single configuration value the installer collects and writes to
    ``~/.ai_guard/config.env``.

    Lives in this lightweight module so agent plugins can declare ``Field``
    instances via :meth:`AgentInstaller.env_fields` without importing the
    installer's UI / CLI stack.
    """

    key: str
    label: str
    default: str | None = None
    secret: bool = False
    tier: int = 1


class AgentInstaller(ABC):
    name: str

    @abstractmethod
    def detect(self) -> bool:
        """Return True if the agent looks installed, else False."""

    @abstractmethod
    def install(self, proxy_url: str) -> list[Path]:
        """Merge our hooks into the agent settings and point env at the proxy."""

    @abstractmethod
    def uninstall(self) -> list[Path]:
        """Remove our hooks from the agent settings."""

    def env_fields(self) -> list[Field]:
        """Return agent-specific config fields to merge into the prompt list."""
        return []
