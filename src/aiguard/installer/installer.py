from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from aiguard import __version__, paths, utils
from aiguard.claude.installer import ClaudeInstaller
from aiguard.constants import AIGuardConstants
from aiguard.installer import ui
from aiguard.installer.agent import AgentInstaller, Field
from aiguard.installer.service import manager as service_manager
from aiguard.storage import load_config, save_config
from aiguard.utils import wait_ready


SUPPORTED_AGENTS: list[AgentInstaller] = [ClaudeInstaller()]

FIELDS: list[Field] = [
    Field("DD_SITE", "Site", default="datadoghq.com", tier=1),
    Field("DD_API_KEY", "API key", default=None, secret=True, tier=1),
    Field("DD_APP_KEY", "Application key", default=None, secret=True, tier=1),
    Field("DD_ENV", "Environment", default="prod", tier=1),
    Field("DD_SERVICE", "Service name", default=None, tier=1),
    Field("DD_VERSION", "Service version", default="1.0", tier=1),
    Field("DD_AI_GUARD_BLOCK", "Block on unsafe verdict (else observe-only)", default="True", tier=2),
    Field("DD_AI_GUARD_PROXY_HOST", "Proxy bind host",default=AIGuardConstants.PROXY_HOST_DEFAULT, tier=2),
    Field("DD_AI_GUARD_PROXY_PORT", "Proxy bind port", default=str(AIGuardConstants.PROXY_PORT_DEFAULT), tier=2),
    Field("DD_TRACE_ENABLED", "Enable tracing", default="True", tier=3),
    Field("DD_AI_GUARD_ENABLED", "Enable AI Guard", default="True", tier=3),
    Field("DD_INSTRUMENTATION_TELEMETRY_ENABLED", "Enable instrumentation telemetry", default="False", tier=3),
    Field("_DD_APM_TRACING_AGENTLESS_ENABLED", "Enable agentless tracer", default="true", tier=3),
]

class MissingRequiredError(RuntimeError):
    def __init__(self, key: str) -> None:
        super().__init__(f"missing required env var {key}")
        self.key = key


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_fields(agents: list[AgentInstaller], tier:int = 1) -> list[Field]:
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
    fields = _find_fields(agents, 3)

    values: dict[str, str] = {}
    for field in fields:
        skip_prompt = non_interactive or field.tier == 3 or (field.tier == 2 and not advanced)
        if skip_prompt:
            env_value = env.get(field.key)
            if env_value:
                values[field.key] = env_value
            elif field.default is not None:
                values[field.key] = field.default
            else:
                raise MissingRequiredError(field.key)
        else:
            values[field.key] = _prompt(field, env)

    return values

def _prompt(field: Field, env: dict[str, str]) -> str:
    """Read one :class:`Field` from the user, falling back to its ``env`` value."""
    label = f"{field.label} ({field.key})"
    env_value = env.get(field.key)

    if env_value:
        # Render the env value (masked for secrets) right after the colon, with
        # the cursor positioned at its end — press Enter to accept, or edit to
        # override. On non-TTY (piped tests), readline pre-fill is a no-op:
        # input() then reads the piped line (empty → accept, otherwise override).
        prefill = ui.mask_secret(env_value) if field.secret else env_value
        typed = ui.prompt_with_value(f"{label}: ", prefill)
        if not typed or typed == prefill:
            return env_value
        return typed

    if field.secret:
        return ui.read_secret(label)

    return ui.prompt_with_default(label, field.default)


def _proxy_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def _ensure_binary_in_place(c: Console) -> None:
    """Guarantee ``~/.local/bin/ai-guard`` exists before we wire the service.

    Two install paths reach this code:

    1. Bootstrap shell script — downloads the binary, drops it at
       ``~/.local/bin/ai-guard``, then execs it. Binary already in place;
       nothing to do.
    2. User runs a frozen binary from somewhere else (e.g. ``./ai-guard
       install`` from a download directory). We copy ourselves to the
       expected location so the wrapper the service backend writes can find
       us.

    Running from source (``uv run ai-guard install``) is rejected with a
    clear error — the wrapper script needs a real binary on disk, not the
    current Python interpreter.
    """
    target = paths.binary_path()
    if target.exists():
        return

    if not getattr(sys, "frozen", False):
        ui.err(c, f"no AI Guard binary at {target}")
        ui.detail(
            c,
            "The installer needs a built AI Guard binary on disk so the "
            "background service can launch it.",
        )
        ui.detail(c, "")
        ui.detail(c, "Run the bootstrap installer (downloads the release artifact):")
        ui.detail(
            c,
            Text(
                "    curl -fsSL https://raw.githubusercontent.com/"
                "DataDog/ai-guard-hooks/main/installer/install.sh | sh",
                style="bold",
            ),
        )
        ui.detail(c, "Or build locally and copy the binary into place:")
        ui.detail(
            c,
            Text(
                f"    uv run pyinstaller ai-guard.spec && cp dist/ai-guard {target}",
                style="bold",
            ),
        )
        sys.exit(1)

    source = Path(sys.executable).resolve()
    if source == target.resolve():
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    os.chmod(target, 0o755)
    ui.detail(c, f"copied running binary into place at {target}")


def _detect_agents(selected: tuple[str, ...]) -> list[AgentInstaller]:
    """Return the subset of :data:`SUPPORTED_AGENTS` to install for this run.

    With no ``--agent`` flag we consider every supported agent; otherwise we
    keep only the ones whose ``name`` the user asked for (unknown names are a
    hard error so typos don't silently install nothing). Either way, an agent
    must report itself as present via ``detect()`` to make the cut.
    """
    if selected:
        unknown = sorted(set(selected) - {a.name for a in SUPPORTED_AGENTS})
        if unknown:
            raise click.BadParameter(f"unknown agent(s): {', '.join(unknown)}")
        candidates = [a for a in SUPPORTED_AGENTS if a.name in selected]
    else:
        candidates = list(SUPPORTED_AGENTS)
    return [a for a in candidates if a.detect()]


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
        ("Service log", service_manager.log_hint()),
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


def _tail_log(c: Console, lines: int = 50) -> None:
    """Show recent service log on readiness failure.

    The service captures stdout/stderr through the platform's standard log
    facility — journald on Linux, unified log on macOS — which catches
    startup crashes that happen before the proxy's own Python logger is set
    up. The service manager owns the platform-specific reader; we just
    panel the output.
    """
    title, body = service_manager.tail_log(lines)
    c.print(Panel(body, title=title, border_style=ui.ERR))


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

    # Make sure the state + bin dirs exist before we ask the user for anything.
    paths.state_dir().mkdir(parents=True, exist_ok=True)
    paths.local_bin_dir().mkdir(parents=True, exist_ok=True)

    # ── Detect ────────────────────────────────────────────────────────────────
    ui.section(c, "Detect coding agents")
    agents = _detect_agents(agents_selected)
    if not agents:
        looked_for = ", ".join(sorted(a.name for a in SUPPORTED_AGENTS))
        ui.err(c, f"no supported coding agents detected (looked for: {looked_for})")
        sys.exit(1)
    for agent in agents:
        if agent.detect():
            ui.ok(c, Text(agent.name, style="bold"))
        else:
            ui.warn(c, Text(agent.name, style="bold"))

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
        for field in _find_fields(agents, tier=2):
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

    proxy_url = _proxy_url(
        values.get("DD_AI_GUARD_PROXY_HOST", AIGuardConstants.PROXY_HOST_DEFAULT),
        int(values.get("DD_AI_GUARD_PROXY_PORT", AIGuardConstants.PROXY_PORT_DEFAULT)),
    )

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
        service_manager.install()
    except Exception as exc:
        ui.err(c, f"failed to register service: {exc}")
        sys.exit(1)

    backend = "launchd LaunchAgent" if utils.is_macos() else "systemd --user unit"
    ui.ok(c, f"{backend} registered")
    ui.detail(c, _service_path())

    port = int(values.get("DD_AI_GUARD_PROXY_PORT", AIGuardConstants.PROXY_PORT_DEFAULT))
    host = values.get("DD_AI_GUARD_PROXY_HOST", AIGuardConstants.PROXY_HOST_DEFAULT)
    ready_host = AIGuardConstants.PROXY_HOST_DEFAULT if host in ("0.0.0.0", "::") else host

    if not wait_ready(ready_host, port, timeout=10.0):
        ui.err(c, f"proxy did not come up on {ready_host}:{port} within 10s")
        _tail_log(c)
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

    if not yes:
        bullets = [
            "Stop and remove the AI Guard service",
            "Remove AI Guard from detected agent configs",
            "Delete ~/.ai_guard/config.env, backups, and session history",
            "Delete the binary at ~/.local/bin/ai-guard",
            "Keep ~/.ai_guard/ai_guard.log* (service log lives in journald / unified log)",
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
        if not agent.detect():
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

    ui.section(c, "Uninstall result")
    _purge_state_dir()
    ui.ok(c, "config + backups + session history removed")

    try:
        paths.wrapper_path().unlink()
        ui.ok(c, f"removed {paths.wrapper_path()}")
    except FileNotFoundError:
        pass

    binary = paths.binary_path()
    if binary.exists():
        _remove_binary(binary)
        ui.ok(c, f"removed {binary}")

    rows: list[tuple[str, str]] = [
        ("App log", f"{paths.log_file_path()}*"),
        ("Service log", service_manager.log_hint()),
    ]
    ui.summary_panel(c, "✓  AI Guard uninstalled", rows, border=ui.OK)
    c.print()


def _remove_binary(binary: Path) -> None:
    # When running as a PyInstaller onefile bundle, the bootloader lazily
    # re-opens the executable to read modules from the embedded archive (see
    # PyInstaller/loader/pyimod01_archive.py). Unlinking it mid-run then raises
    # "appears to have been moved or deleted since this application was
    # launched" on the next import. Defer the unlink to a detached helper so
    # we can exit cleanly first.
    if getattr(sys, "frozen", False):
        subprocess.Popen(
            ["sh", "-c", f'sleep 1; rm -f -- "{binary}"'],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    binary.unlink()


def _purge_state_dir() -> None:
    """Remove everything under ~/.ai_guard except the application log files.

    The proxy's rotating application log (``ai_guard.log*``) is preserved so
    the user keeps a forensic trail after uninstall. Service stdout/stderr
    no longer lives on disk under ``~/.ai_guard`` — it's in journald / the
    macOS unified log, both of which outlive ``ai-guard uninstall``
    independently.
    """
    state = paths.state_dir()
    if not state.exists():
        return
    keep_prefixes = ("ai_guard.log",)
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