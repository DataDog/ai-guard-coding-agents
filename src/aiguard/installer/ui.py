"""Visual primitives for the installer CLI.

Centralised here so the installer/uninstaller code stays readable and the
palette / spacing / iconography are consistent across every screen the user
sees. Built on :mod:`rich`; degrades to plain text in non-TTYs and when
``NO_COLOR`` is set or ``--no-color`` is passed.

Style conventions
-----------------

* **Accent** — a single Datadog-ish purple (``#774AA4``) used for the banner
  title, section rules, and the summary panel border. Everything else is
  white / dim / status-coloured so the eye knows where to land.
* **Status markers** — ``✓`` (green, success), ``⚠`` (yellow, advisory),
  ``✗`` (red, failure), ``→`` (cyan, action), ``·`` (dim, muted).
* **Hierarchy** — section rule for phase boundaries, two-space indent for
  status lines, four-space indent for sub-detail. Vertical breathing room
  before every phase.

Value entry — :func:`read_secret`, :func:`prompt_with_value`,
:func:`prompt_with_default`, :func:`mask_secret` — also lives here so every
piece of user-facing IO (banner, prompts, summaries) shares a single module.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable

import click
from rich.align import Align
from rich.console import Console
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

ACCENT = "#774AA4"  # Datadog purple
ACCENT_DIM = "#5a3680"
OK = "green"
WARN = "yellow"
ERR = "red"
ACTION = "cyan"


def console(no_color: bool) -> Console:
    """Build the installer's :class:`Console` honouring ``NO_COLOR`` / ``--no-color``."""
    return Console(
        no_color=no_color or bool(os.environ.get("NO_COLOR")),
        highlight=False,
        soft_wrap=False,
    )


# ── Top-level chrome ──────────────────────────────────────────────────────────


def section(c: Console, label: str) -> None:
    """Phase divider: ``─── Detect coding agents ───────────────────``."""
    c.print()
    c.print(Rule(Text(label, style=f"bold {ACCENT}"), style=ACCENT_DIM, align="left"))
    c.print()


# ── Status lines ──────────────────────────────────────────────────────────────


def _line(c: Console, marker: str, marker_style: str, body: Text | str, indent: int = 2) -> None:
    line = Text()
    line.append(marker, style=marker_style)
    line.append("  ")
    if isinstance(body, Text):
        line.append_text(body)
    else:
        line.append(body)
    c.print(Padding(line, (0, 0, 0, indent)))


def ok(c: Console, message: str | Text, indent: int = 2) -> None:
    _line(c, "✓", OK, message, indent)


def warn(c: Console, message: str | Text, indent: int = 2) -> None:
    _line(c, "⚠", WARN, message, indent)


def err(c: Console, message: str | Text, indent: int = 2) -> None:
    _line(c, "✗", ERR, message, indent)


def action(c: Console, message: str | Text, indent: int = 2) -> None:
    _line(c, "→", ACTION, message, indent)


def detail(c: Console, message: str | Text, indent: int = 5) -> None:
    """Sub-line under a status marker (no marker, dim color)."""
    body = message if isinstance(message, Text) else Text(message, style="dim")
    c.print(Padding(body, (0, 0, 0, indent)))


def hint(c: Console, message: str) -> None:
    """A grey 'tip' line. Used for paths and shell snippets in the closing notes."""
    c.print(Padding(Text(message, style="dim"), (0, 0, 0, 2)))


def hints_table(c: Console, rows: Iterable[tuple[str, str]]) -> None:
    """A small two-column dim table of label → command tips."""
    table = Table.grid(padding=(0, 3), expand=False)
    table.add_column(style="dim", justify="left", no_wrap=True)
    table.add_column(style="dim", no_wrap=False)
    for label, command in rows:
        table.add_row(label, command)
    c.print(Padding(table, (0, 0, 0, 2)))


# ── Summary panel ─────────────────────────────────────────────────────────────


def summary_panel(
    c: Console,
    title: str,
    rows: Iterable[tuple[str, str]],
    *,
    border: str = ACCENT,
) -> None:
    """Two-column key/value table inside a coloured panel, padded for breathing room."""
    table = Table.grid(padding=(0, 3), expand=False)
    table.add_column(style="dim", justify="left", no_wrap=True)
    table.add_column(no_wrap=False)
    for label, value in rows:
        table.add_row(label, value)
    c.print()
    c.print(
        Panel(
            Align.left(table),
            title=Text(title, style=f"bold {border}"),
            border_style=border,
            padding=(1, 3),
            expand=False,
        )
    )


# ── Confirmation prompts ──────────────────────────────────────────────────────


def confirm_block(c: Console, intent: str, bullets: list[str]) -> None:
    """Render the 'this is what's about to happen' preamble before a y/N prompt."""
    c.print()
    c.print(Padding(Text(intent, style="bold"), (0, 2)))
    c.print()
    for b in bullets:
        c.print(Padding(Text(f"• {b}", style="dim"), (0, 0, 0, 4)))
    c.print()


# ── Value entry ───────────────────────────────────────────────────────────────


def mask_secret(value: str) -> str:
    """Render ``value`` with all but the last 4 chars replaced by ``*``.

    Always match the original value's length so the user gets a length hint;
    reveal the last 4 chars only when the value is long enough that they can't
    be brute-forced back to the full secret.
    """
    if len(value) > 8:
        return "*" * (len(value) - 4) + value[-4:]
    return "*" * len(value)


def read_secret(label: str) -> str:
    """Prompt for a secret with per-character asterisk echo.

    Uses :mod:`pwinput` on a real TTY (``termios`` on Unix, ``msvcrt`` on
    Windows). When stdin is not a TTY — tests, ``CliRunner``,
    ``DD_API_KEY=... | curl … | sh`` pipes — ``pwinput``'s raw-mode call into
    ``termios.tcgetattr`` would raise; fall back to ``click.prompt`` with
    ``hide_input=True`` which uses ``getpass`` and works on any stream.
    """
    if not sys.stdin.isatty():
        return click.prompt(label, hide_input=True, confirmation_prompt=False)

    import pwinput

    return pwinput.pwinput(prompt=f"{label}: ", mask="*")


def prompt_with_value(label: str, value: str) -> str:
    # ``readline.redisplay()`` inside the startup hook would double-draw the
    # line — readline already renders prompt + buffer when the hook returns, so
    # an explicit redisplay produces the prompt twice on captured/non-ANSI
    # outputs. Just seed the buffer and let readline display it.
    try:
        import readline
    except ImportError:  # pragma: no cover - Windows or stripped builds
        return input(label)

    readline.set_startup_hook(lambda: readline.insert_text(value))
    try:
        return input(label)
    finally:
        readline.set_startup_hook()


def prompt_with_default(label: str, default_value: str | None) -> str:
    return click.prompt(label, default=default_value, show_default=True)
