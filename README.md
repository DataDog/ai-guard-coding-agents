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

### Linux

Supported: `x86_64` and `arm64`. Requires `curl` (or `wget`), `sha256sum` (or `shasum`), and a user systemd instance (
`systemctl --user`).

```sh
curl -fsSL https://raw.githubusercontent.com/DataDog/ai-guard-hooks/main/installer/install.sh | sh
```

### macOS

Supported: Apple Silicon (`arm64`). Requires `curl` and `shasum` (both ship with macOS).

```sh
curl -fsSL https://raw.githubusercontent.com/DataDog/ai-guard-hooks/main/installer/install.sh | sh
```

### Windows

Coming soon via `install.ps1`.

### What gets modified

| Path                                                  | Purpose                                                         | When            | Agent    |
|-------------------------------------------------------|-----------------------------------------------------------------|-----------------|----------|
| `~/.local/bin/ai-guard`                               | The CLI binary.                                                 | `linux` `macOS` | `*`      |
| `~/.local/bin/ai-guard-service`                       | Service wrapper that launchd/systemd execs.                     | `linux` `macOS` | `*`      |
| `~/.ai_guard/config.env`                              | Configured values (mode `0600`).                                | `linux` `macOS` | `*`      |
| `~/.ai_guard/backups/`                                | Pre-install copies of every file the installer touches.         | `linux` `macOS` | `*`      |
| `~/.ai_guard/ai_guard.log`                            | Rotating application log from the tool.                         | `linux` `macOS` | `*`      |
| `~/.ai_guard/ai_guard_service.log`                    | stdout/stderr captured by launchd/systemd.                      | `linux` `macOS` | `*`      |
| `~/.config/systemd/user/ai-guard.service`             | systemd `--user` unit, enabled and started.                     | `linux`         | `*`      |
| `~/Library/LaunchAgents/com.datadoghq.ai-guard.plist` | launchd `LaunchAgent`, loaded with `launchctl`.                 | `macOS`         | `*`      |
| `~/.claude/settings.json`                             | Hook block added under `hooks.*` plus `env.ANTHROPIC_BASE_URL`. | `linux` `macOS` | `Claude` |

Nothing is written outside `$HOME`. No root, no `sudo`, no system-wide service.

### Uninstall

```sh
ai-guard uninstall
```

This stops and unregisters the service, restores `~/.claude/settings.json` from the backup, and removes config, backups,
session history, the service unit, the wrapper, and the binary. Only `ai_guard.log*` and `ai_guard_service.log*` remain
under `~/.ai_guard/` so you can keep a forensic trail.

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
