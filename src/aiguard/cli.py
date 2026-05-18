"""AI Guard CLI"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import click

from aiguard.hooks.hooks import hook

for _key in [k for k in os.environ if k.startswith("OTEL_")]:
    del os.environ[_key]

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "aiguard"

# Module-level imports below run AFTER the OTEL_ env cleanup and sys.path
# patch above — that's intentional, so E402 is silenced for these lines.
from ddtrace import tracer  # noqa: E402

from aiguard import __version__  # noqa: E402
from aiguard.proxy.server import proxy  # noqa: E402

logger = logging.getLogger("ai_guard")


def _setup_logging(log_file: str | None) -> None:
    if not log_file:
        logger.addHandler(logging.NullHandler())
        return
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_file,
        mode="a",
        maxBytes=1_000_000,
        backupCount=10,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    def _excepthook(exc_type, exc_value, exc_tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            logger.critical("uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook


class _Group(click.Group):
    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        formatter.write_usage(ctx.command_path, "AGENT HOOK [OPTIONS]")

    def invoke(self, ctx: click.Context) -> object:
        try:
            return super().invoke(ctx)
        finally:
            tracer.shutdown()


@click.group(cls=_Group)
@click.version_option(version=__version__, prog_name="ai-guard")
@click.option(
    "--log-file",
    envvar="DD_AI_GUARD_LOG_FILE",
    default=lambda: str(Path.home() / ".ai_guard" / "ai_guard.log"),
    show_default=True,
    help="Path to log file.",
)
def main(log_file: str | None) -> None:
    """Datadog AI Guard — real-time security for coding agents.

    Intercepts and evaluates every agent action (prompts, tool calls,
    responses) through Datadog AI Guard, blocking unsafe operations
    before they execute.

    \b
    Commands:
      hook    Dispatch a hook event for an agent
      proxy   Transparent HTTP proxy for inspecting LLM traffic

    \b
    Examples:
      ai-guard hook claude SessionStart < event.json
      ai-guard proxy --port 29279 --anthropic-upstream https://api.anthropic.com
    """
    _setup_logging(log_file)


main.add_command(hook)
main.add_command(proxy)

if __name__ == "__main__":
    main()
