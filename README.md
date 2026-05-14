# AI Guard for Coding Agents

Real-time security guardrails for AI coding agents, powered by [Datadog AI Guard](https://docs.datadoghq.com/security/ai_guard/).

When an AI coding agent like Claude Code reads a file, runs a command, or loads a skill or plugin, that content can carry a prompt-injection payload, an instruction to exfiltrate secrets, or other hostile behaviour. AI Guard for Coding Agents watches the session as it happens and blocks dangerous activity before it runs.

## What it protects against

- **Prompt injection** smuggled into files, web pages, or tool outputs the agent reads.
- **Secret exfiltration** — credentials, tokens, and customer data being sent to attacker-controlled destinations.
- **Malicious skills** and plugins that try to load themselves into the agent's session.
- **Unsafe shell or file operations** the model has been tricked into running.

Every decision is logged to Datadog so your security team has a full audit trail of what the agent tried to do, what was blocked, and why.

## How your developers experience it

- The agent works the way it always has — no new commands to learn, no extra prompts to remember.
- When something risky comes through, the agent sees a clear block message and explains it back to the user in plain English, including the most likely risk category and a recommended next step.
- Sessions that aren't risky are never interrupted.

## Supported agents

| Agent            | Status         |
|------------------|----------------|
| Claude Code      | Available      |
| OpenAI Codex CLI | On the roadmap |
| Cursor           | On the roadmap |

## Installation

> **TODO:** Installation instructions will be published here once the distribution channel is finalized (Homebrew tap, signed installer, container image, etc.).

## Configuration

Environment settings to configure:

| Setting                    | What it does                                                                                                                       |
|----------------------------|------------------------------------------------------------------------------------------------------------------------------------|
| `DD_API_KEY`, `DD_APP_KEY` | Your Datadog credentials. Required.                                                                                                |
| `DD_SERVICE`, `DD_ENV`     | How this deployment appears in Datadog.                                                                                            |
| `DD_AI_GUARD_BLOCK`        | `true` to block on policy violations, `false` to observe only — events still ship to Datadog but the agent is never interrupted.   |

## Viewing decisions in Datadog

Every AI Guard evaluation (allowed or blocked) is sent to your Datadog account. You can:

- Browse the timeline of each coding session.
- See which tool calls were flagged and what the risk category was.
- Investigate trends across your team or your CI fleet.

See the [Datadog AI Guard documentation](https://docs.datadoghq.com/security/ai_guard/) for screenshots and dashboards.

## Support

Questions, feature requests, or a suspected bug? Please open an issue on this repository or reach out to your Datadog account team.

## License

Apache 2.0 — see [LICENSE](LICENSE).