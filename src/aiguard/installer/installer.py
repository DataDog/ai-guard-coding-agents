from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.text import Text

from aiguard import keychain, paths, utils
from aiguard.claude.installer import ClaudeInstaller
from aiguard.constants import AIGuardConstants
from aiguard.installer import ui
from aiguard.installer.agent import AgentInstaller, Field, Tier
from aiguard.installer.service import manager as service_manager
from aiguard.storage import save_config

logger = logging.getLogger("ai_guard")

SUPPORTED_AGENTS: list[AgentInstaller] = [ClaudeInstaller()]

FIELDS: list[Field] = [
    Field("DD_SITE", "Site", default="datadoghq.com", tier=Tier.REQUIRED),
    Field("DD_API_KEY", "API key", default=None, secret=True, tier=Tier.REQUIRED),
    Field("DD_APP_KEY", "Application key", default=None, secret=True, tier=Tier.REQUIRED),
    Field("DD_ENV", "Environment", default="prod", tier=Tier.REQUIRED),
    Field("DD_SERVICE", "Service name", default=None, tier=Tier.REQUIRED),
    Field("DD_VERSION", "Service version", default="1.0", tier=Tier.REQUIRED),
    Field(
        "DD_AI_GUARD_BLOCK",
        "Block on unsafe verdict (else observe-only)",
        default="True",
        tier=Tier.ADVANCED,
    ),
    Field("DD_TRACE_ENABLED", "Enable tracing", default="True", tier=Tier.SILENT),
    Field("DD_AI_GUARD_ENABLED", "Enable AI Guard", default="True", tier=Tier.SILENT),
    Field(
        "DD_INSTRUMENTATION_TELEMETRY_ENABLED",
        "Enable instrumentation telemetry",
        default="False",
        tier=Tier.SILENT,
    ),
    Field(
        "_DD_APM_TRACING_AGENTLESS_ENABLED",
        "Enable agentless tracer",
        default="true",
        tier=Tier.SILENT,
    ),
    Field(
        "DD_AI_GUARD_LOG_LEVEL",
        "Default logging level",
        default="ERROR",
        tier=Tier.SILENT,
    ),
    Field(
        "DD_TRACE_REPORT_HOSTNAME",
        "Report the hostname in traces",
        default="True",
        tier=Tier.SILENT,
    ),
    Field(
        "DD_AI_GUARD_PRIVACY_MODE",
        "Privacy mode (CODING_AGENT shows message contents in the UI only for "
        "blocked evaluations; DEFAULT shows them for every evaluation)",
        default=AIGuardConstants.PRIVACY_MODE_CODING_AGENT,
        tier=Tier.SILENT,
    ),
]


class MissingRequiredError(RuntimeError):
    def __init__(self, key: str) -> None:
        super().__init__(f"missing required env var {key}")
        self.key = key


# ── Helpers ───────────────────────────────────────────────────────────────────


def _find_fields(agents: list[AgentInstaller], tier: Tier = Tier.REQUIRED) -> list[Field]:
    seen: set[str] = set()
    fields: list[Field] = []
    for field in FIELDS:
        if field.key not in seen and field.tier <= tier:
            fields.append(field)
            seen.add(field.key)
    for agent in agents:
        for field in agent.env_fields():
            if field.key not in seen and field.tier <= tier:
                fields.append(field)
                seen.add(field.key)
    return fields


def _collect_fields(
    *,
    advanced: bool,
    non_interactive: bool,
    env: dict[str, str],
    agents: list[AgentInstaller],
) -> dict[str, str]:
    """Return the full config values map, prompting where appropriate."""
    fields = _find_fields(agents, Tier.PASSTHROUGH)

    values: dict[str, str] = {}
    for field in fields:
        skip_prompt = (
            non_interactive
            or field.tier >= Tier.SILENT
            or (field.tier == Tier.ADVANCED and not advanced)
        )
        if skip_prompt:
            env_value = env.get(field.key)
            if env_value:
                values[field.key] = env_value
            elif field.default is not None:
                values[field.key] = field.default
            elif field.tier == Tier.PASSTHROUGH:
                # No env value, no default — passthrough fields are saved only
                # when the user actually sets them. Skip silently.
                continue
            else:
                raise MissingRequiredError(field.key)
        else:
            values[field.key] = _prompt(field, env)

    return values


def _prompt(field: Field, env: dict[str, str]) -> str:
    """Read one :class:`Field` from the user, falling back to its ``env`` value."""
    label = f"{field.label} ({field.key})"
    env_value = env.get(field.key)

    # Stored/exported values must win over the field's hardcoded default, otherwise
    # reinstall silently resets customised entries when the user hits Enter.
    default = env_value or field.default
    return ui.prompt(label, default, field.secret)


def _ensure_binary_in_place(c: Console) -> None:
    """Guarantee the onedir bundle is in place before we wire the service.

    The PyInstaller bundle is laid out as a directory (``onedir`` mode), so we
    don't have a single self-contained binary to move around — the launcher
    needs its ``_internal/`` siblings next to it. Two install paths reach
    this code:

    1. Bootstrap shell script — downloads the release tarball, extracts the
       bundle into ``~/.local/share/ai-guard``, symlinks the launcher at
       ``~/.local/bin/ai-guard``, then execs the symlink. Bundle already in
       place; nothing to do.
    2. User runs a frozen binary from somewhere else (e.g. ``./ai-guard
       install`` from a freshly built ``dist/ai-guard``). We copy the whole
       bundle into ``bundle_dir()`` and symlink the launcher at
       ``binary_path()`` so the service wrapper can find a stable path.

    Running from source (``uv run ai-guard install``) is rejected with a
    clear error — the wrapper script needs a real binary on disk, not the
    current Python interpreter.
    """
    bundle = paths.bundle_dir()
    target = paths.binary_path()
    bundle_exec = paths.bundle_executable()
    if bundle_exec.exists() and target.exists():
        return

    if not getattr(sys, "frozen", False):
        ui.err(c, f"no AI Guard bundle at {bundle}")
        ui.detail(
            c,
            "The installer needs a built AI Guard bundle on disk so the "
            "background service can launch it.",
        )
        ui.detail(c, "")
        ui.detail(c, "Run the bootstrap installer (downloads the release artifact):")
        ui.detail(
            c,
            Text(
                "    curl -fsSL https://raw.githubusercontent.com/"
                "DataDog/ai-guard-coding-agents/main/scripts/install.sh | sh",
                style="bold",
            ),
        )
        ui.detail(c, "Or build locally with uv + pyinstaller and install from the tarball:")
        ui.detail(
            c,
            Text(
                "    sh scripts/build.sh && "
                "AI_GUARD_BUNDLE=$(pwd)/dist/ai-guard.tar.gz sh scripts/install.sh",
                style="bold",
            ),
        )
        sys.exit(1)

    source_bundle = Path(sys.executable).resolve().parent
    if source_bundle != bundle.resolve():
        bundle.parent.mkdir(parents=True, exist_ok=True)
        if bundle.exists():
            shutil.rmtree(bundle)
        shutil.copytree(source_bundle, bundle, symlinks=True)
        os.chmod(bundle_exec, 0o755)
        ui.detail(c, f"copied bundle into place at {bundle}")

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists():
        target.unlink()
    target.symlink_to(bundle_exec)
    ui.detail(c, f"symlinked launcher at {target}")


def _detect_agents(selected: tuple[str, ...]) -> list[AgentInstaller]:
    """Return the subset of :data:`SUPPORTED_AGENTS` to install for this run.

    With no ``--agent`` flag we consider every supported agent; otherwise we
    keep only the ones whose ``name`` the user asked for (unknown names are a
    hard error so typos don't silently install nothing).
    """
    if selected:
        unknown = sorted(set(selected) - {a.name for a in SUPPORTED_AGENTS})
        if unknown:
            raise click.BadParameter(f"unknown agent(s): {', '.join(unknown)}")
        candidates = [a for a in SUPPORTED_AGENTS if a.name in selected]
    else:
        candidates = list(SUPPORTED_AGENTS)
    return candidates


def _path_warning(c: Console) -> None:
    bin_dir = str(paths.local_bin_dir())
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    if bin_dir not in path_entries:
        ui.warn(c, f"{bin_dir} is not on your PATH")
        ui.detail(c, Text(f'export PATH="{bin_dir}:$PATH"', style="bold"))


def _remove_legacy_service() -> None:
    """Silently tear down a proxy service left by an older install, if present."""
    try:
        service_manager.uninstall()
    except Exception:
        logger.debug("could not fully remove legacy proxy service", exc_info=True)


def _summary(
    c: Console,
    *,
    agent_updates: dict[str, list[Path]],
    keychained: list[str],
) -> None:
    secrets_loc = (
        f"OS keychain ({', '.join(keychained)})" if keychained else str(paths.config_env_path())
    )
    rows: list[tuple[str, str]] = [
        ("Binary", str(paths.binary_path())),
        ("Config", str(paths.config_env_path())),
        ("Keys", secrets_loc),
        ("App log", str(paths.log_file_path())),
    ]

    if agent_updates:
        lines: list[str] = []
        for name, files in agent_updates.items():
            lines.append(name)
            for path in files:
                lines.append(f"  {path}")
        rows.append(("Agents", "\n".join(lines)))
    else:
        rows.append(("Agents", "(none)"))

    ui.summary_panel(c, "✓  AI Guard ready", rows, border=ui.OK)

    c.print()
    hints: list[tuple[str, str]] = [
        ("Edit config", f"$EDITOR {paths.config_env_path()}"),
        ("Uninstall", "ai-guard uninstall"),
    ]
    ui.hints_table(c, hints)
    c.print()


# ── install ────────────────────────────────────────────────────────────────────


@click.command("install")
@click.option("--advanced", is_flag=True, help="Prompt for tier-2 configuration too.")
@click.option(
    "--non-interactive",
    is_flag=True,
    help="Read all required values from the environment; fail if any are missing.",
)
@click.option(
    "--agent",
    "agents_selected",
    multiple=True,
    help="Restrict installation to specific agents. Validated against the "
    "registered agent set at call time.",
)
@click.option("--no-color", is_flag=True, help="Disable ANSI colour output.")
def install(
    advanced: bool,
    non_interactive: bool,
    agents_selected: tuple[str, ...],
    no_color: bool,
) -> None:
    """Install ai-guard: wire the agent's hooks and write config."""
    c = ui.console(no_color)

    if not (utils.is_macos() or utils.is_linux()):
        ui.err(c, f"unsupported platform: {sys.platform}")
        sys.exit(1)

    # Make sure the config, state, and bin dirs exist before we ask the user
    # for anything. Config and state live under XDG_CONFIG_HOME and
    # XDG_STATE_HOME respectively.
    paths.ai_guard_config_dir().mkdir(parents=True, exist_ok=True)
    paths.state_dir().mkdir(parents=True, exist_ok=True)
    paths.local_bin_dir().mkdir(parents=True, exist_ok=True)

    # ── Detect ────────────────────────────────────────────────────────────────
    ui.section(c, "Detect coding agents")
    candidates = _detect_agents(agents_selected)
    agents = []
    for agent in candidates:
        supported, message = agent.detect()
        label = Text(agent.name, style="bold")
        if supported:
            ui.ok(c, label)
            agents.append(agent)
        else:
            ui.warn(c, label)
        if message:
            ui.detail(c, message)
    if not agents:
        ui.err(c, "no supported coding agents available — address the warnings above and re-run")
        sys.exit(1)

    # ── Configure ─────────────────────────────────────────────────────────────
    ui.section(c, "Configure")
    # The CLI has already merged config.env and any keychain-stored secrets into
    # the environment (stored values as a base, anything exported live taking
    # precedence), so prompt straight from os.environ — re-running with a key
    # re-exported changes it.
    try:
        values = _collect_fields(
            advanced=advanced,
            non_interactive=non_interactive,
            env=dict(os.environ),
            agents=agents,
        )
    except MissingRequiredError as exc:
        ui.err(c, str(exc))
        sys.exit(2)

    if non_interactive:
        ui.ok(c, "values sourced from the environment")
        for field in _find_fields(agents, tier=Tier.ADVANCED):
            value = values.get(field.key)
            if value is None:
                continue
            shown = ui.mask_secret(value) if field.secret else value
            ui.detail(c, f"{field.key} = {shown}")

    # ── Write config ──────────────────────────────────────────────────────────
    ui.section(c, "Write configuration")
    # Route DD_API_KEY / DD_APP_KEY to the OS keychain when one is reachable so
    # they never sit in plaintext config.env. keychain.store() returns False on
    # a host with no keychain (headless Linux); those keys stay in the file.
    # Rewriting config.env without the keychained keys also migrates installs
    # that previously stored them in the file.
    secret_values = [k for k in keychain.SECRET_KEYS if values.get(k)]
    keychained = [k for k in secret_values if keychain.store(k, values[k])]
    config_values = {k: v for k, v in values.items() if k not in keychained}
    save_config(config_values)
    body = Text()
    body.append(str(paths.config_env_path()))
    body.append("  ")
    body.append("(mode 0600)", style="dim")
    ui.ok(c, body)
    if keychained:
        ui.ok(c, f"{', '.join(keychained)} stored in the OS keychain")
    elif secret_values:
        ui.warn(c, "no OS keychain available — API & app keys saved to config.env")

    # ── Install hooks ───────────────────────────────────────────────────────────
    # The ``ai-guard hook`` commands wired into the agent run in-process, so the
    # launcher just needs to be on disk — there is no proxy service to start.
    ui.section(c, "Install hooks")
    _ensure_binary_in_place(c)
    agent_updates = {}
    for agent in agents:
        updated = agent.install()
        agent_updates[agent.name] = updated
        ui.ok(c, Text(agent.name, style="bold"))
        for path in updated:
            ui.detail(c, str(path))

    # Silently tear down a proxy service left by an older install — most users
    # never had one, so we don't surface it in the output.
    _remove_legacy_service()

    _path_warning(c)
    _summary(c, agent_updates=agent_updates, keychained=keychained)


# ── uninstall ──────────────────────────────────────────────────────────────────


@click.command("uninstall")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--no-color", is_flag=True, help="Disable ANSI colour output.")
def uninstall(yes: bool, no_color: bool) -> None:
    """Uninstall ai-guard: remove hooks and state, tear down any legacy service.

    The application log is preserved.
    """
    c = ui.console(no_color)
    ui.section(c, "Uninstall")
    if not yes:
        bullets = [
            "Remove AI Guard from detected agent configs",
            f"Delete {paths.config_env_path()}",
            "Remove the API & app keys from the OS keychain",
            "Delete the binary at ~/.local/bin/ai-guard",
            f"Keep {paths.log_file_path()}* (application logs)",
        ]
        ui.confirm_block(c, "About to:", bullets)
        if not click.confirm("Continue?", default=False):
            ui.warn(c, "aborted")
            sys.exit(1)

    # Silently tear down a proxy service left by an older install (see install).
    _remove_legacy_service()

    ui.section(c, "Restored")
    agent_updates: dict[str, list[Path]] = {}
    for agent in SUPPORTED_AGENTS:
        # Drive rollback off "settings still reference ai-guard", not
        # "the agent is supported": a user who downgraded Claude below the
        # min version (or removed it) after install must still get a clean
        # uninstall — otherwise the stale hook block in settings.json keeps
        # pointing at a binary we then delete below.
        if not agent.is_installed():
            continue
        try:
            updated = agent.uninstall()
            agent_updates[agent.name] = updated
            ui.ok(c, Text(agent.name, style="bold"))
            for path in updated:
                ui.detail(c, str(path))
        except Exception as exc:
            ui.warn(c, f"{agent.name}: {exc}")
    if not agent_updates:
        ui.detail(c, "(no installed agents detected)")

    ui.section(c, "Result")
    _purge_state_dir()
    ui.ok(c, "config removed")

    for key in keychain.SECRET_KEYS:
        keychain.delete(key)
    ui.ok(c, "API & app keys removed from the OS keychain")

    try:
        paths.wrapper_path().unlink()
        ui.ok(c, f"removed {paths.wrapper_path()}")
    except FileNotFoundError:
        pass

    binary = paths.binary_path()
    bundle = paths.bundle_dir()
    if binary.is_symlink() or binary.exists():
        # The PATH-visible launcher is just a symlink; removing it is safe
        # while we're still running (it's only metadata).
        try:
            binary.unlink()
        except FileNotFoundError:
            pass
        ui.ok(c, f"removed {binary}")
    if bundle.exists():
        _remove_bundle(bundle)
        ui.ok(c, f"removed {bundle}")

    rows: list[tuple[str, str]] = [
        ("App log", f"{paths.log_file_path()}*"),
    ]
    ui.summary_panel(c, "✓  AI Guard uninstalled", rows, border=ui.OK)
    c.print()


def _remove_bundle(bundle: Path) -> None:
    # When running as a frozen PyInstaller bundle, the launcher and ``_internal/``
    # files are still mapped by this process; unlinking them mid-run can break
    # lazy imports. Defer the ``rm -rf`` to a detached helper so we can exit
    # cleanly first.
    if getattr(sys, "frozen", False):
        subprocess.Popen(
            ["sh", "-c", f'sleep 1; rm -rf -- "{bundle}"'],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    shutil.rmtree(bundle, ignore_errors=True)


def _purge_state_dir() -> None:
    """Remove our config dir; preserve the app log.

    The rotating application log (``ai-guard.log*``) is left in place
    so the user keeps a forensic trail after uninstall.
    """
    config = paths.ai_guard_config_dir()
    if config.exists():
        shutil.rmtree(config, ignore_errors=True)

    state = paths.state_dir()
    if not state.exists():
        return
    keep_prefixes = ("ai-guard.log",)
    for entry in state.iterdir():
        if entry.is_file() and entry.name.startswith(keep_prefixes):
            continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            try:
                entry.unlink()
            except FileNotFoundError:
                pass
