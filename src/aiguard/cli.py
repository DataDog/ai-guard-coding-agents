# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""AI Guard CLI"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import click

from aiguard import __version__, keychain, paths, storage
from aiguard.hooks.hooks import hook
from aiguard.installer.installer import install, uninstall

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "aiguard"

logger = logging.getLogger("ai_guard")


def _setup_logging(log_file: str | None, log_level: str) -> None:
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
    logger.setLevel(getattr(logging, log_level.upper(), logging.ERROR))

    def _excepthook(exc_type, exc_value, exc_tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            logger.critical("uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook


class _Group(click.Group):
    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        formatter.write_usage(ctx.command_path, "AGENT HOOK [OPTIONS]")


@click.group(cls=_Group)
@click.version_option(version=__version__, prog_name="ai-guard")
def main() -> None:
    """Datadog AI Guard — real-time security for coding agents.

    Intercepts and evaluates every agent action (prompts, tool calls,
    responses) through Datadog AI Guard, blocking unsafe operations
    before they execute.

    Configuration (credentials, ``DD_AI_GUARD_LOG_FILE`` / ``DD_AI_GUARD_LOG_LEVEL``,
    block mode, …) is read from the environment and from ``config.env``
    (``$XDG_CONFIG_HOME/ai-guard/config.env``); exported values take precedence.

    \b
    Commands:
      hook       Dispatch a hook event for an agent
      install    Set up ai-guard for detected coding agents
      uninstall  Remove ai-guard and restore agent configs

    \b
    Examples:
      ai-guard hook claude SessionStart < event.json
      ai-guard install
      ai-guard uninstall --yes
    """
    # Make config.env + keychain secrets available to every command (and the
    # hooks) before anything reads the environment.
    storage.load_into_environ()
    keychain.load_into_env()
    _setup_logging(
        os.environ.get("DD_AI_GUARD_LOG_FILE", str(paths.log_file_path())),
        os.environ.get("DD_AI_GUARD_LOG_LEVEL", "ERROR"),
    )


main.add_command(hook)
main.add_command(install)
main.add_command(uninstall)

if __name__ == "__main__":
    main()
