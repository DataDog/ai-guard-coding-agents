"""Per-agent installer plugin interface.

Each supported coding agent ships a subclass that knows how to detect itself,
inject the ai-guard hook block, and reverse the change. The installer layer
above is agnostic to the specifics.
"""

from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


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
    def detect(self) -> bool:
        """Return True if the agent looks installed, else False."""

    @abstractmethod
    def install(self, proxy_url: str) -> list[Path]:
        """Merge our hooks into the agent settings and point env at the proxy."""

    @abstractmethod
    def uninstall(self) -> list[Path]:
        """Remove our hooks from the agent settings."""

    def env_fields(self) -> tuple[Field, ...]:
        """Return agent-specific config fields to merge into the prompt list."""
        return ()


def _which(cmd: str) -> Path | None:
    found = shutil.which(cmd)
    return Path(found) if found else None


def detect_executable(name: str) -> Path | None:
    return _which(name)


def detect_env_var(key: str) -> str | None:
    value = os.environ.get(key)
    return value if value else None
