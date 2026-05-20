"""Interactive prompting for ``ai-guard install``.

Three groups of configuration values:

* **Tier 1** is always prompted (the user types ``DD_API_KEY``, ``DD_APP_KEY``,
  and ``DD_SERVICE`` every install). Secrets echo as ``*`` per character so the
  user can see the length of what they have typed and notice obvious slips,
  while the cleartext never lands in the terminal scrollback.
* **Tier 2** is prompted only with ``--advanced``; otherwise each value lands
  in ``config.env`` with its default. Tier 2 is split between generic knobs
  (``DD_SITE``, ``DD_ENV``, proxy host/port, …) and per-agent fields the
  installer asks every detected agent for. The Anthropic upstream URL only
  appears in a Claude install — :class:`AgentInstaller.env_fields` is the
  extension point.
* **Silent** values are never prompted at all — they always take the hard-coded
  default. ``DD_TRACE_ENABLED`` and ``DD_AI_GUARD_ENABLED`` are silent because
  flipping them off makes the whole tool a no-op, so there is no install-time
  configuration to ask the user about.

In ``--non-interactive`` mode every Tier 1 value must come from the
environment; if any is missing the caller surfaces a clear error and exits.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import click

from aiguard.constants import AIGuardConstants
from aiguard.installer.agent import Field

if TYPE_CHECKING:
    from aiguard.installer.agent import AgentInstaller


def _read_secret(label: str) -> str:
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


TIER1: tuple[Field, ...] = (
    Field("DD_API_KEY", "Datadog API key", default=None, secret=True, tier=1),
    Field("DD_APP_KEY", "Datadog application key", default=None, secret=True, tier=1),
    Field("DD_SERVICE", "Datadog service name", default="ai-guard", tier=1),
)


SILENT_DEFAULTS: dict[str, str] = {
    "DD_TRACE_ENABLED": "True",
    "DD_AI_GUARD_ENABLED": "True",
    "DD_INSTRUMENTATION_TELEMETRY_ENABLED": "False",
    "_DD_APM_TRACING_AGENTLESS_ENABLED": "True",
}


def tier2_generic_fields() -> tuple[Field, ...]:
    """Generic tier-2 fields that apply regardless of which agent is detected."""
    return (
        Field("DD_SITE", "Datadog site", default="datadoghq.com", tier=2),
        Field("DD_ENV", "Datadog environment", default="prod", tier=2),
        Field("DD_VERSION", "Datadog service version", default="1.0", tier=2),
        Field(
            "DD_AI_GUARD_BLOCK",
            "Block on unsafe verdict (else observe-only)",
            default="True",
            tier=2,
        ),
        Field(
            "DD_AI_GUARD_PROXY_HOST",
            "Proxy bind host",
            default=AIGuardConstants.PROXY_HOST_DEFAULT,
            tier=2,
        ),
        Field(
            "DD_AI_GUARD_PROXY_PORT",
            "Proxy bind port",
            default=str(AIGuardConstants.PROXY_PORT_DEFAULT),
            tier=2,
        ),
    )


def _tier2_fields(agents: list[AgentInstaller], detected_upstream: str | None) -> tuple[Field, ...]:
    """Generic tier-2 fields + each detected agent's own contribution.

    Order matters only for the prompt UX (we want generic settings first, then
    agent-specific). Duplicate keys across agents are dropped — first agent
    wins — so two agents can't fight over the same env var.
    """
    seen: set[str] = set()
    fields: list[Field] = []
    for field in tier2_generic_fields():
        if field.key not in seen:
            fields.append(field)
            seen.add(field.key)
    for agent in agents:
        for field in agent.env_fields(detected_upstream):
            if field.key not in seen:
                fields.append(field)
                seen.add(field.key)
    return tuple(fields)


class MissingRequiredError(RuntimeError):
    def __init__(self, key: str) -> None:
        super().__init__(f"missing required env var {key}")
        self.key = key


def _prompt_one(field: Field, env: dict[str, str]) -> str:
    env_value = env.get(field.key)
    if env_value:
        return env_value
    if field.secret:
        return _read_secret(field.label)
    return click.prompt(field.label, default=field.default, show_default=True)


def collect(
    *,
    advanced: bool,
    non_interactive: bool,
    detected_upstream: str | None,
    env: dict[str, str],
    agents: list[AgentInstaller] | None = None,
) -> dict[str, str]:
    """Return the full config values map, prompting where appropriate.

    ``agents`` is the list of installers that ``ai-guard install`` actually
    detected. Each one contributes zero-or-more tier-2 :class:`Field`\\ s via
    :meth:`AgentInstaller.env_fields`; the installer only asks for agent
    fields when that agent is being wired up.
    """
    agent_list: list[AgentInstaller] = list(agents or [])
    tier2 = _tier2_fields(agent_list, detected_upstream)

    fields: list[Field] = list(TIER1)
    if advanced:
        fields.extend(tier2)

    values: dict[str, str] = {}
    for field in fields:
        if non_interactive:
            env_value = env.get(field.key)
            if env_value:
                values[field.key] = env_value
            elif field.default is not None:
                values[field.key] = field.default
            else:
                raise MissingRequiredError(field.key)
        else:
            values[field.key] = _prompt_one(field, env)

    if not advanced:
        # Ensure tier-2 values land in config.env with their defaults so the
        # proxy service has a complete environment to source.
        for field in tier2:
            if field.key not in values:
                if field.key in env:
                    values[field.key] = env[field.key]
                elif field.default is not None:
                    values[field.key] = field.default

    # Silent values are never prompted: env-provided overrides win, otherwise
    # the hard-coded default lands in config.env so the service has a complete
    # environment to source.
    for key, default in SILENT_DEFAULTS.items():
        values.setdefault(key, env.get(key) or default)

    return values
