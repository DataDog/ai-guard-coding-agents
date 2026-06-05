# AI Guard for Coding Agents

![Claude Code](https://img.shields.io/badge/Claude_Code-ready-success?style=flat-square&logo=anthropic&logoColor=white)
![Codex CLI](https://img.shields.io/badge/Codex_CLI-roadmap-lightgrey?style=flat-square&logo=https%3A%2F%2Fraw.githubusercontent.com%2FDataDog%2Fai-guard-coding-agents%2Fmain%2Fdocs%2Fimages%2Fopenai.svg)
![Cursor](https://img.shields.io/badge/Cursor-roadmap-lightgrey?style=flat-square&logo=cursor&logoColor=white)

A CLI that runs AI coding agent actions through [Datadog AI Guard](https://docs.datadoghq.com/security/ai_guard/) before
they are executed.

When a coding agent reads a file, runs a command, or loads a skill or plugin, that content can carry malicious intent:
prompt-injection payloads, instructions to exfiltrate secrets, attempts to install hostile tools, and similar. This CLI
hooks into the agent's lifecycle, evaluates each tool call against AI Guard, and denies the operation when policy is
violated.

Denied tool calls provide a useful remediation to the user so they can clearly see what steps are required to fix the
issue. Setting `DD_AI_GUARD_BLOCK=false` switches to observe-only: evaluations are still emitted, but no decision is
enforced. Every evaluation (allow or deny) is emitted to Datadog with the session, tool, model, and risk category
attached.

<img src="docs/images/demo.gif" alt="AI Guard for Coding Agents demo" width="680">

## Installation

A single command bootstraps the CLI on Linux and macOS — it downloads the latest signed binary, verifies its SHA-256
checksum, and registers a per-user background service. Everything lives under `$HOME`: no root, no `sudo`, no
system-wide changes.

### Quick start

```sh
curl -fsSL https://raw.githubusercontent.com/DataDog/ai-guard-coding-agents/main/scripts/install.sh -o install.sh
sh install.sh
```

Windows support is coming via `install.ps1`.

### Supported platforms

| Platform | Architectures           | Background service       |
|----------|-------------------------|--------------------------|
| Linux    | `x86_64`, `arm64`       | `systemd --user` unit    |
| macOS    | Apple Silicon (`arm64`) | `launchd` LaunchAgent    |

### Requirements

The bootstrap script checks for these upfront and exits with a clear error if any are missing.

- **HTTP downloader** — `curl` or `wget`
- **Checksum tool** — `sha256sum` or `shasum`
- **Archive tools** — `tar` and `mktemp`
- **Service manager** — `systemctl --user` on Linux, `launchctl` on macOS

### What gets installed

Every path the installer creates or modifies is listed below — nothing else on your machine is touched.

| Path                                                               | Purpose                                                      | OS      | Agent         |
|--------------------------------------------------------------------|--------------------------------------------------------------|---------|---------------|
| `~/.local/share/ai-guard/`                                         | PyInstaller onedir bundle (launcher + `_internal/`).         | `*`     | `*`           |
| `~/.local/bin/ai-guard`                                            | Symlink to the bundle launcher.                              | `*`     | `*`           |
| `~/.local/bin/ai-guard-service`                                    | Wrapper script invoked by launchd / systemd.                 | `*`     | `*`           |
| `${XDG_STATE_HOME:-~/.local/state}/ai-guard/ai-guard.log`          | Rotating application log.                                    | `*`     | `*`           |
| `${XDG_STATE_HOME:-~/.local/state}/ai-guard/<agent>/<session_id>/` | Per-session message history used to build AI Guard requests. | `*`     | `*`           |
| `${XDG_CONFIG_HOME:-~/.config}/ai-guard/config.env`                | Persisted configuration values (mode `0600`).                | `*`     | `*`           |
| `${XDG_CONFIG_HOME:-~/.config}/systemd/user/ai-guard.socket`       | Listening socket, enabled with `systemctl --user`.           | `linux` | `*`           |
| `${XDG_CONFIG_HOME:-~/.config}/systemd/user/ai-guard.service`      | Service activated on demand by `ai-guard.socket`.            | `linux` | `*`           |
| `~/Library/LaunchAgents/com.datadoghq.ai-guard.plist`              | LaunchAgent loaded via `launchctl bootstrap`.                | `macOS` | `*`           |
| `${CLAUDE_CONFIG_DIR:-~/.claude}/settings.json`                    | Hook block under `hooks.*` plus `env.ANTHROPIC_BASE_URL`.    | `*`     | `Claude Code` |

Paths follow the [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir-spec/latest/) and honour
`$XDG_CONFIG_HOME` / `$XDG_STATE_HOME` if set.

Service output is captured by the proxy's rotating logger at `~/.local/state/ai-guard/ai-guard.log`, including uncaught
Python exceptions.

### Uninstall

```sh
ai-guard uninstall
```

Stops and unregisters the service, restores the original coding agent config, and removes all AI Guard artifacts.
`~/.local/state/ai-guard/ai-guard.log*` is preserved as a forensic trail.

## Privacy notice

`DD_AI_GUARD_PRIVACY_MODE` controls how much of the coding trajectory is surfaced in the Datadog AI Guard UI. 

| Value          | Behavior                                                                                                  |
|----------------|-----------------------------------------------------------------------------------------------------------|
| `CODING_AGENT` | *Default*. Message contents are shown only for denied evaluations; on allowed calls results are stripped. |
| `DEFAULT`      | Full conversation and message contents are shown for every evaluation.                                    |


## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and the PR workflow. For an in-depth tour of the codebase,
see [AGENTS.md](AGENTS.md).

## Support

For questions, feature requests, or bug reports, open an issue on this repository.

For security issues, follow the responsible-disclosure process at <https://www.datadoghq.com/security/>. Do not open a
public GitHub issue.

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Third-party components bundled into the released binary are
tracked in [LICENSE-3rdparty.csv](LICENSE-3rdparty.csv).
