from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from aiguard import __version__
from aiguard.claude.installer import ClaudeInstaller
from aiguard.constants import AIGuardConstants
from aiguard.installer import backup, config, paths, prompt, ui
from aiguard.installer.agent import AgentInstaller
from aiguard.installer.service import manager as service_manager
from aiguard.installer.service.readiness import wait_ready

AGENT_CLASSES: dict[str, type[AgentInstaller]] = {
    "claude": ClaudeInstaller,
}


# ── Helpers ───────────────────────────────────────────────────────────────────


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
        ui.err(c, f"no ai-guard binary at {target}")
        ui.detail(
            c,
            "The installer needs a built ai-guard binary on disk so the "
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
    names = selected or tuple(AGENT_CLASSES.keys())
    found: list[AgentInstaller] = []
    for name in names:
        cls = AGENT_CLASSES.get(name)
        if cls is None:
            raise click.BadParameter(f"unknown agent: {name}")
        agent = cls()
        if agent.detect() is not None:
            found.append(agent)
    return found


def _detect_upstream(agents: list[AgentInstaller]) -> str | None:
    """Find a pre-existing upstream URL we should chain through, if any.

    Each agent module decides what counts as "its" upstream (e.g. Claude
    Code's ``env.ANTHROPIC_BASE_URL``); we just take the first non-empty
    answer that isn't already our own proxy URL.
    """
    proxy_url = AIGuardConstants.PROXY_URL_DEFAULT
    for agent in agents:
        try:
            existing = agent.detect_upstream()
        except Exception:
            continue
        if existing and existing != proxy_url:
            return existing
    env_value = os.environ.get("ANTHROPIC_BASE_URL")
    if env_value and env_value != proxy_url:
        return env_value
    return None


def _path_warning(c: Console) -> None:
    bin_dir = str(paths.local_bin_dir())
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    if bin_dir not in path_entries:
        ui.warn(c, f"{bin_dir} is not on your PATH")
        ui.detail(c, Text(f'export PATH="{bin_dir}:$PATH"', style="bold"))


def _service_path() -> str:
    return str(paths.launchd_plist_path() if paths.is_macos() else paths.systemd_unit_path())


def _summary(
    c: Console,
    *,
    installed_agents: list[AgentInstaller],
    chained_upstream: str | None,
) -> None:
    rows: list[tuple[str, str]] = [
        ("Binary", str(paths.binary_path())),
        ("Config", str(paths.config_env_path())),
        ("App log", str(paths.log_file_path())),
        ("Service log", str(paths.service_log_file_path())),
        ("Backups", str(paths.backups_dir())),
        ("Service", _service_path()),
        ("Agents wired", ", ".join(a.name for a in installed_agents) or "(none)"),
    ]
    if chained_upstream:
        rows.append(("Chained upstream", chained_upstream))

    ui.summary_panel(c, "✓  ai-guard ready", rows, border=ui.OK)

    c.print()
    hints: list[tuple[str, str]] = [
        ("Edit config", f"$EDITOR {paths.config_env_path()}"),
        ("Uninstall", "ai-guard uninstall"),
    ]
    ui.hints_table(c, hints)
    c.print()


def _tail_log(c: Console, lines: int = 50) -> None:
    """Tail the service log on readiness failure.

    The service log captures stdout/stderr from launchd/systemd — including
    startup crashes that happen before the proxy's own logger is set up, so
    it is the more useful surface for "why didn't the proxy come up" than
    the rotating application log.
    """
    log = paths.service_log_file_path()
    if not log.exists():
        ui.detail(c, "(service log file not yet created)")
        return
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        ui.detail(c, f"(could not read log: {exc})")
        return
    tail = text.splitlines()[-lines:]
    c.print(Panel("\n".join(tail) or "(empty)", title=str(log), border_style=ui.ERR))


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
    ui.banner(
        c,
        title="ai-guard installer",
        subtitle="Real-time security guardrails for your coding agents.",
        version=__version__,
    )

    if not (paths.is_macos() or paths.is_linux()):
        ui.err(c, f"unsupported platform: {sys.platform}")
        sys.exit(1)

    # Make sure the state + bin dirs exist before we ask the user for anything.
    paths.state_dir().mkdir(parents=True, exist_ok=True)
    paths.local_bin_dir().mkdir(parents=True, exist_ok=True)

    # ── Detect ────────────────────────────────────────────────────────────────
    ui.section(c, "Detect coding agents")
    agents = _detect_agents(agents_selected)
    if not agents:
        looked_for = ", ".join(sorted(AGENT_CLASSES.keys()))
        ui.err(c, f"no supported coding agents detected (looked for: {looked_for})")
        sys.exit(1)
    for agent in agents:
        path = agent.detect()
        ui.ok(c, Text(agent.name, style="bold"))
        if path is not None:
            ui.detail(c, str(path))

    detected_upstream = _detect_upstream(agents)
    if detected_upstream:
        msg = Text()
        msg.append("chaining existing upstream  ")
        msg.append(detected_upstream, style="dim")
        ui.action(c, msg)

    # ── Configure ─────────────────────────────────────────────────────────────
    ui.section(c, "Configure")
    # Re-install: stored values become defaults; real env vars override them
    # so the user can change a key by re-exporting it before re-running.
    stored = config.read()
    if stored:
        ui.action(c, f"reusing {len(stored)} value(s) from {paths.config_env_path()}")
    merged_env = {**stored, **os.environ}
    try:
        values = prompt.collect(
            advanced=advanced,
            non_interactive=non_interactive,
            detected_upstream=detected_upstream,
            env=merged_env,
            agents=agents,
        )
    except prompt.MissingRequiredError as exc:
        ui.err(c, str(exc))
        sys.exit(2)

    if non_interactive:
        ui.ok(c, "values sourced from the environment")

    # ── Write config ──────────────────────────────────────────────────────────
    ui.section(c, "Write configuration")
    config.write(values)
    body = Text()
    body.append(str(paths.config_env_path()))
    body.append("  ")
    body.append("(mode 0600)", style="dim")
    ui.ok(c, body)

    proxy_url = paths.proxy_url(
        values.get("DD_AI_GUARD_PROXY_HOST", AIGuardConstants.PROXY_HOST_DEFAULT),
        int(values.get("DD_AI_GUARD_PROXY_PORT", AIGuardConstants.PROXY_PORT_DEFAULT)),
    )

    # ── Install hooks ─────────────────────────────────────────────────────────
    ui.section(c, "Install hooks")
    msg = Text()
    msg.append("proxy at ")
    msg.append(proxy_url, style="dim")
    ui.action(c, msg)
    for agent in agents:
        result = agent.install_hooks(proxy_url)
        backup.record_install(
            agent.name,
            result.settings_path,
            result.restore_data,
        )
        ui.ok(c, Text(agent.name, style="bold"))
        ui.detail(c, str(result.settings_path))

    # ── Service ───────────────────────────────────────────────────────────────
    ui.section(c, "Register service")
    _ensure_binary_in_place(c)
    try:
        service_manager.install()
    except Exception as exc:
        ui.err(c, f"failed to register service: {exc}")
        sys.exit(1)

    backend = "launchd LaunchAgent" if paths.is_macos() else "systemd --user unit"
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
    _summary(c, installed_agents=agents, chained_upstream=detected_upstream)


# ── uninstall ──────────────────────────────────────────────────────────────────


@click.command("uninstall")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--no-color", is_flag=True, help="Disable ANSI colour output.")
def uninstall(yes: bool, no_color: bool) -> None:
    """Uninstall ai-guard: remove hooks, service, and state. Logs remain."""
    c = ui.console(no_color)
    ui.banner(
        c,
        title="ai-guard uninstaller",
        subtitle="Remove ai-guard cleanly; logs stay behind for forensics.",
        version=__version__,
    )

    if not yes:
        bullets = [
            "Stop and remove the ai-guard service",
            "Remove ai-guard hooks from detected agent configs",
            "Delete ~/.ai_guard/config.env, backups, and session history",
            "Delete the binary at ~/.local/bin/ai-guard",
            "Keep ~/.ai_guard/ai_guard.log* and ~/.ai_guard/ai_guard_service.log*",
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

    ui.section(c, "Remove hooks")
    removed_any = False
    for agent_name in backup.all_agents():
        record = backup.load_install(agent_name)
        if not record:
            continue
        cls = AGENT_CLASSES.get(agent_name)
        if cls is None:
            continue
        agent = cls(settings_path=Path(record["settings_path"]))  # type: ignore[call-arg]
        try:
            agent.uninstall_hooks(record.get("restore_data") or {})
            ui.ok(c, Text(agent_name, style="bold"))
            ui.detail(c, record["settings_path"])
            removed_any = True
        except Exception as exc:
            ui.warn(c, f"{agent_name}: {exc}")
    if not removed_any:
        ui.detail(c, "(no installed agents recorded)")

    ui.section(c, "Clean state")
    _purge_state_dir()
    ui.ok(c, "config + backups + session history removed")

    try:
        paths.wrapper_path().unlink()
        ui.ok(c, f"removed {paths.wrapper_path()}")
    except FileNotFoundError:
        pass

    try:
        paths.binary_path().unlink()
        ui.ok(c, f"removed {paths.binary_path()}")
    except FileNotFoundError:
        pass

    rows: list[tuple[str, str]] = [
        ("App log", f"{paths.log_file_path()}*"),
        ("Service log", f"{paths.service_log_file_path()}*"),
    ]
    ui.summary_panel(c, "✓  ai-guard uninstalled", rows, border=ui.OK)
    c.print()


def _purge_state_dir() -> None:
    """Remove everything under ~/.ai_guard except the log files.

    Both ``ai_guard.log*`` (proxy's rotating application log) and
    ``ai_guard_service.log*`` (launchd/systemd stdout/stderr capture) are
    preserved so the user keeps a forensic trail after uninstall.
    """
    state = paths.state_dir()
    if not state.exists():
        return
    keep_prefixes = ("ai_guard.log", "ai_guard_service.log")
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