"""Bundled template files for service registration."""

from __future__ import annotations

from importlib.resources import files
from string import Template


def render(name: str, **substitutions: str) -> str:
    """Load template ``name`` (relative to this package) and substitute vars."""
    raw = files(__package__).joinpath(name).read_text(encoding="utf-8")
    return Template(raw).substitute(**substitutions)
