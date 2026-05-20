# AI Guard for Coding Agents

![Claude Code](https://img.shields.io/badge/Claude_Code-ready-success?style=flat-square&logo=anthropic&logoColor=white)
![Codex CLI](https://img.shields.io/badge/Codex_CLI-roadmap-lightgrey?style=flat-square&logo=https%3A%2F%2Fraw.githubusercontent.com%2FDataDog%2Fai-guard-coding-agents%2Fmain%2Fdocs%2Fimages%2Fopenai.svg)
![Cursor](https://img.shields.io/badge/Cursor-roadmap-lightgrey?style=flat-square&logo=cursor&logoColor=white)

A CLI that runs AI coding agent actions through [Datadog AI Guard](https://docs.datadoghq.com/security/ai_guard/) before they are executed.

When a coding agent reads a file, runs a command, or loads a skill or plugin, that content can carry malicious intent: prompt-injection payloads, instructions to exfiltrate secrets, attempts to install hostile tools, and similar. This CLI hooks into the agent's lifecycle, evaluates each tool call against AI Guard, and denies the operation when policy is violated.

Denied tool calls provide a useful remediation to the user so they can clearly see what steps are required to fix the issue. Setting `DD_AI_GUARD_BLOCK=false` switches to observe-only: evaluations are still emitted, but no decision is enforced. Every evaluation (allow or deny) is emitted to Datadog with the session, tool, model, and risk category attached.

<img src="docs/images/demo.gif" alt="AI Guard for Coding Agents demo" width="680">

## Installation

> **TODO:** Installation instructions will be published here once the distribution channel is finalized (Homebrew tap, signed installer, container image, etc.).

## Configuration

Environment settings to configure:

| Setting             | What it does                                                                      |
|---------------------|-----------------------------------------------------------------------------------|
| `DD_API_KEY`        | Datadog API key. Required.                                                        |
| `DD_APP_KEY`        | Datadog application key. Required.                                                |
| `DD_SERVICE`        | Service tag attached to every emitted span.                                       |
| `DD_ENV`            | Environment tag attached to every emitted span.                                   |
| `DD_AI_GUARD_BLOCK` | `true` to block on policy violations, `false` to observe only.                    |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and the PR workflow. For an in-depth tour of the codebase, see [AGENTS.md](AGENTS.md).

## Support

For questions, feature requests, or bug reports, open an issue on this repository.

For security issues, follow the responsible-disclosure process at <https://www.datadoghq.com/security/>. Do not open a public GitHub issue.

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Third-party components bundled into the released binary are tracked in [LICENSE-3rdparty.csv](LICENSE-3rdparty.csv).
