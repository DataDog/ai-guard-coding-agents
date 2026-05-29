from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.text import Text

from aiguard import paths, utils
from aiguard.claude.installer import ClaudeInstaller
from aiguard.constants import AIGuardConstants
from aiguard.installer import ui
from aiguard.installer.agent import AgentInstaller, Field, Tier
from aiguard.installer.service import manager as service_manager
from aiguard.storage import load_config, save_config
from aiguard.utils import wait_ready

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
    Field(
        "DD_AI_GUARD_PROXY_HOST",
        "Proxy bind host",
        default=AIGuardConstants.PROXY_HOST_DEFAULT,
        tier=Tier.ADVANCED,
    ),
    Field(
        "DD_AI_GUARD_PROXY_PORT",
        "Proxy bind port",
        default=str(AIGuardConstants.PROXY_PORT_DEFAULT),
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
        "DD_AI_GUARD_PROXY_IDLE_TIMEOUT",
        "Shut down after N seconds with no requests (0 keeps the LLM proxy running forever)",
        default="300",
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


def _proxy_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


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


def _service_path() -> str:
    return str(paths.launchd_plist_path() if utils.is_macos() else paths.systemd_unit_path())


def _summary(
    c: Console,
    *,
    agent_updates: dict[str, list[Path]],
) -> None:
    rows: list[tuple[str, str]] = [
        ("Binary", str(paths.binary_path())),
        ("Config", str(paths.config_env_path())),
        ("App log", str(paths.log_file_path())),
        ("Service", _service_path()),
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
    """Install ai-guard: wire hooks, write config, start the proxy service."""
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
    # Re-install: stored values become defaults; real env vars override them
    # so the user can change a key by re-exporting it before re-running.
    stored = load_config()
    if stored:
        ui.action(c, f"reusing {len(stored)} value(s) from {paths.config_env_path()}")
    merged_env = {**stored, **os.environ}
    try:
        values = _collect_fields(
            advanced=advanced,
            non_interactive=non_interactive,
            env=merged_env,
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
    save_config(values)
    body = Text()
    body.append(str(paths.config_env_path()))
    body.append("  ")
    body.append("(mode 0600)", style="dim")
    ui.ok(c, body)

    host = values.get("DD_AI_GUARD_PROXY_HOST", AIGuardConstants.PROXY_HOST_DEFAULT)
    port = int(values.get("DD_AI_GUARD_PROXY_PORT", AIGuardConstants.PROXY_PORT_DEFAULT))
    proxy_url = _proxy_url(host, port)

    # ── Install ─────────────────────────────────────────────────────────
    ui.section(c, "Install")
    msg = Text()
    msg.append("proxy at ")
    msg.append(proxy_url, style="dim")
    ui.action(c, msg)
    agent_updates = {}
    for agent in agents:
        updated = agent.install(proxy_url)
        agent_updates[agent.name] = updated
        ui.ok(c, Text(agent.name, style="bold"))
        for path in updated:
            ui.detail(c, str(path))

    # ── Service ───────────────────────────────────────────────────────────────
    ui.section(c, "Register service")
    _ensure_binary_in_place(c)
    try:
        service_manager.install(host, port)
    except Exception as exc:
        ui.err(c, f"failed to register service: {exc}")
        sys.exit(1)

    backend = "launchd LaunchAgent" if utils.is_macos() else "systemd --user unit"
    ui.ok(c, f"{backend} registered")
    ui.detail(c, _service_path())

    ready_host = AIGuardConstants.PROXY_HOST_DEFAULT if host in ("0.0.0.0", "::") else host

    if not wait_ready(ready_host, port, timeout=10.0):
        ui.err(c, f"proxy did not come up on {ready_host}:{port} within 10s")
        sys.exit(1)
    body = Text()
    body.append("proxy responding on ")
    body.append(f"{ready_host}:{port}", style="dim")
    ui.ok(c, body)

    _path_warning(c)
    _summary(c, agent_updates=agent_updates)


# ── uninstall ──────────────────────────────────────────────────────────────────


@click.command("uninstall")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--no-color", is_flag=True, help="Disable ANSI colour output.")
def uninstall(yes: bool, no_color: bool) -> None:
    """Uninstall ai-guard: remove hooks, service, and state. Logs remain."""
    c = ui.console(no_color)
    ui.section(c, "Uninstall")
    if not yes:
        bullets = [
            "Stop and remove the AI Guard service",
            "Remove AI Guard from detected agent configs",
            f"Delete {paths.config_env_path()} and session history",
            "Delete the binary at ~/.local/bin/ai-guard",
            f"Keep {paths.log_file_path()}* (proxy logs)",
        ]
        ui.confirm_block(c, "About to:", bullets)
        if not click.confirm("Continue?", default=False):
            ui.warn(c, "aborted")
            sys.exit(1)

    ui.section(c, "Stop service")
    try:
        service_manager.uninstall()
        ui.ok(c, "service stopped and unregistered")
    except Exception as exc:
        ui.warn(c, f"service uninstall reported: {exc}")

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
    ui.ok(c, "config + session history removed")

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
    """Remove our config dir and per-session history; preserve the app log.

    The proxy's rotating application log (``ai-guard.log*``) is left in place
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
