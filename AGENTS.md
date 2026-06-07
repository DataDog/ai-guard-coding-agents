# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, Cursor, …) working in this repository.

## Purpose

Real-time guardrails for coding agents. The CLI wires hooks into the agent's lifecycle and evaluates every tool call against [Datadog AI Guard](https://docs.datadoghq.com/security/ai_guard/) **in-process** — there is no proxy and no background service.

`ai-guard hook AGENT HOOK` is the one moving piece: a short-lived command the agent invokes on each lifecycle event (wired into Claude Code via `~/.claude/settings.json`). It reads the event JSON from stdin, rebuilds the conversation from the agent's own transcript, evaluates the pending tool call, and writes a decision back to stdout — an empty body to allow, or a JSON deny/block payload to stop the call.

## Repository layout

```
/
├── src/aiguard/
│   ├── cli.py                      # Top-level Click CLI: hook, install, uninstall.
│   │                               #   Group callback loads config.env into the env + sets up logging.
│   ├── constants.py                # AIGuardConstants — span names + tag keys
│   ├── storage.py                  # config.env read/write: load_config / save_config / load_into_environ.
│   │                               #   No conversation storage — history comes from the agent's transcript.
│   ├── paths.py                    # XDG path helpers (bundle, launcher, config.env, Claude settings dir)
│   ├── utils.py                    # atomic_write, platform predicates, fetch_endpoint_id, detect_executable
│   ├── hooks/
│   │   └── hooks.py                # Handler ABC + `ai-guard hook` command (in-process dispatch to a Handler)
│   ├── claude/
│   │   ├── handler.py              # ClaudeHandler(Handler): handle_hook dispatch, hook methods,
│   │   │                           #   transcript → AI Guard messages, blocked-tool payloads
│   │   └── installer.py            # ClaudeInstaller(AgentInstaller): merge/remove the hook block in settings.json
│   └── installer/
│       ├── installer.py            # `install` / `uninstall` Click commands + tiered field collection
│       ├── agent.py                # AgentInstaller ABC + Field / Tier
│       ├── ui.py                   # rich-based prompts and output
│       └── service/                # Teardown ONLY of a legacy proxy service (manager / launchd / systemd_user / wrapper)
├── tests/
│   ├── conftest.py                 # Fixtures (tmp_home, fake_ai_guard, tracer_recorder, transcripts, …)
│   ├── transcripts.py              # TranscriptWriter + builders for Claude JSONL transcripts
│   ├── unit/{test_claude, test_cli, test_storage, test_installer, test_utils}.py
│   └── integration/{test_hooks, test_binary_hooks}.py
├── docker/claude/
│   ├── Dockerfile                  # Multi-stage: PyInstaller binary + Claude Code runtime
│   ├── claude-settings.json        # Wires every Claude Code hook to `ai-guard hook claude <Event>`
│   └── entrypoint.sh               # Container entrypoint
├── .github/workflows/
│   ├── test.yml                    # Cross-platform test + binary smoke
│   └── build.yml                   # Tagged release build (calls test.yml first)
├── ai-guard.spec                   # PyInstaller spec — onedir bundle
├── pyproject.toml                  # uv project metadata, optional `test`/`build` extras
├── CONTRIBUTING.md                 # PR workflow, dev setup, header convention
├── LICENSE                         # Apache 2.0
├── LICENSE-3rdparty.csv            # Direct deps tracked for OSS compliance
└── NOTICE                          # Apache 2.0 attribution
```

## Key entry points

| Command | Source | What it does |
|---|---|---|
| `ai-guard hook <AGENT> <HOOK>` | `hooks/hooks.py:hook` | Read the event JSON from stdin, build the agent's `Handler`, evaluate against AI Guard, and write any decision to stdout. **Always exits 0** so a failed hook never breaks the host agent's command flow. |
| `ai-guard install` | `installer/installer.py:install` | Detect supported agents, collect + write `config.env`, place the launcher on disk, and merge the hook block into the agent's settings. |
| `ai-guard uninstall` | `installer/installer.py:uninstall` | Remove the hook block, delete the config + binary, preserve the application log. |

`cli.main`'s group callback runs before every subcommand: it loads `config.env` into the environment (`storage.load_into_environ`, exported values win) and configures logging from `DD_AI_GUARD_LOG_FILE` / `DD_AI_GUARD_LOG_LEVEL`. Block mode comes from `DD_AI_GUARD_BLOCK` (default on); there is no `--block` flag.

## How a hook flows end-to-end

```
Claude Code hook subprocess
   │   stdin = event JSON (transcript_path, session_id, [agent_id], tool_name, …)
   ▼
ai-guard hook claude PreToolUse              # cli.main loads config.env → env; hooks/hooks.py
   │   _build_handler("claude", block) → ClaudeHandler
   ▼
ClaudeHandler.handle_hook("PreToolUse", payload)     # claude/handler.py
   │   maps "PreToolUse" → method "_pre_tool_use", calls it
   ▼
_pre_tool_use(event)
   │   _set_common_tags(event)                         # span tags via @tracer.wrap
   │   messages = _load_messages(transcript_path, agent_id)   # read the agent's transcript
   │   self._ai_guard.evaluate(messages, Options(block=…, tags=…))
   ▼
AIGuardAbortError → _blocked_tool_response(...) → JSON deny/block to stdout
otherwise          → return None → empty stdout (allowed)
```

Dispatch is dynamic: `getattr(self, "_" + camel_to_snake(hook), None)`. To add a hook, add a method named `_<snake_case>` and the handler will route to it. Handler methods are synchronous and return `dict | None`.

## Conversation history (transcripts, not storage)

The handler rebuilds history from **Claude Code's own JSONL transcripts** — ai-guard keeps no per-session storage of its own.

- Main session: `<project>/<session_id>.jsonl` (the `transcript_path` the hook payload carries).
- Subagent (sidechain): `<project>/<session_id>/subagents/agent-<agent_id>.jsonl`.

`_load_messages(transcript_path, agent_id)` (`claude/handler.py`) selects the right file — the subagent transcript when `agent_id` is set, else the main one — parses each JSONL line, and maps `user`/`assistant` turns to AI Guard `Message`s: `tool_use` blocks become `tool_calls`, `tool_result` blocks become `role="tool"` messages, text/thinking become content parts. Metadata rows and malformed lines are skipped. `_append_tool_result` adds the `PostToolUse` result/error unless the transcript already contains it (so a flushed result isn't duplicated).

## Currently wired hooks (Claude)

Method names map 1:1 to the event names in `docker/claude/claude-settings.json`:

| Hook event | Method | What it does |
|---|---|---|
| `SessionStart` | `_session_start` | Emits a span with session_id / model / user tags. |
| `SessionEnd` | `_session_end` | Emits a span. (No stored history to clear — history lives in the agent's transcript.) |
| `SubagentStart` | `_subagent_start` | Emits a span with subagent_id / subagent_type tags. |
| `SubagentStop` | `_subagent_stop` | Emits a span with subagent_id / subagent_type tags. |
| `PreToolUse` | `_pre_tool_use` | Loads the transcript; for `Skill` calls injects the resolved `SKILL.md` body as a tool message so AI Guard sees what is about to load. On `AIGuardAbortError` returns `{"hookSpecificOutput": {"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":…,"additionalContext":…}}` — `permissionDecisionReason` is the branded TUI banner, `additionalContext` is the structured guidance for Claude. |
| `PostToolUse` | `_post_tool_use` | Appends the tool result to the transcript history and re-evaluates. On abort returns both `{"decision":"block","reason":…}` (TUI banner) and `hookSpecificOutput.additionalContext`. |
| `PostToolUseFailure` | `_post_tool_use_failure` | Same shape as PostToolUse but uses `event.error` as the tool content. |

## Adding a new Claude Code hook

1. Add a method `_<snake_case_hook_name>` on `ClaudeHandler`. Decorate with `@tracer.wrap(name=…, resource=AIGuardConstants.HOOK_RESOURCE)` if you want a span. Signature: `def _foo(self, event: dict[str, Any]) -> dict[str, Any] | None`.
2. Register the hook in `docker/claude/claude-settings.json` so Claude Code invokes `ai-guard hook claude <CamelCaseName>` on the right lifecycle event.
3. Add a test in `tests/unit/test_claude.py` (`TestHandleHookSpans` or `TestHandleHookToolUse`).

If the hook needs new span tags or operation names, add constants in `src/aiguard/constants.py`.

## Adding a new agent (e.g., Codex, Cursor)

1. Create `src/aiguard/<agent>/handler.py` with a `Handler` subclass (`hooks/hooks.py`):
   - `agent()` — lowercase name used in `ai-guard hook <agent> …`.
   - `handle_hook(hook, body) -> bytes` — dispatch the event and return the agent-shaped response (empty to allow).
2. Register it in `hooks/hooks.py:_build_handler` (lazy import so unrelated commands don't pull in the agent's deps).
3. For installer support, add an `AgentInstaller` subclass (`installer/agent.py`) that wires/removes the agent's hook config, and register it in `installer/installer.py:SUPPORTED_AGENTS`.
4. Add hidden imports for the new module to `ai-guard.spec`, and a wiring file under `docker/<agent>/`.

## Installer

`install` merges the hook block into the agent's `settings.json` (no env redirect — the hooks run in-process) and places the launcher under `~/.local/share/ai-guard` with a `~/.local/bin/ai-guard` symlink so the `ai-guard hook` command resolves. It does **not** register a background service.

Both `install` and `uninstall` **silently** tear down any proxy service left by an older version — most users never had one, so it never appears in the output. The `installer/service/` backends only implement teardown (`uninstall`/`remove`); there is no install path. `uninstall` also removes the hook block, `config.env`, and the binary, and strips any legacy `env.ANTHROPIC_BASE_URL` redirect from `settings.json`; the application log is preserved.

Config is collected into `config.env` via tiered `Field`s (`installer/installer.py`). The CLI loads that file into the environment on every run, so the hooks (which build the AI Guard client from `DD_API_KEY` / `DD_APP_KEY` / `DD_SITE`) and logging pick it up.

## Build & test

```bash
# install both extras (test deps + pyinstaller)
uv sync --extra test --extra build

# unit + integration (skips binary-marker tests if dist/ai-guard isn't built)
uv run pytest -q

# binary integration tests (needs dist/ai-guard or AI_GUARD_BINARY set)
uv run pyinstaller ai-guard.spec --noconfirm
uv run pytest -m binary -v

# lint
uv run ruff check src/ tests/
```

CI (`.github/workflows/test.yml`) runs the source suite and a binary smoke test across the platforms in `.github/matrix.json` (Linux x86_64 + arm64, macOS arm64, Windows x86_64), then `.github/workflows/build.yml` reuses the same matrix to publish per-platform PyInstaller artifacts on tagged releases.

## Conventions

- Python 3.11+, managed with `uv`. Line length 100, double quotes, `ruff` for linting.
- `Message` / `ContentPart` / `ToolCall` / `Function` come from `ddtrace.appsec.ai_guard`. They're `TypedDict`s — use the constructors for clarity but expect plain dicts after JSON round-trips.
- Hook handler methods are synchronous, return `dict | None`, and are decorated with `@tracer.wrap`. Set tags via `tracer.current_span()`, not by manually opening a span.
- Conversation history is the agent's transcript — ai-guard does not persist its own copy. Read it through `_load_messages`; don't build a parallel store.
- Logging goes to `$XDG_STATE_HOME/ai-guard/ai-guard.log` (rotating, 1 MB × 10 backups). Hook stdout must contain **only** the JSON decision the host agent expects — no extra prints.
- The `ddtrace` dependency is pinned to a custom branch via a direct reference in `project.dependencies` (`ddtrace @ git+https://github.com/DataDog/dd-trace-py@malvarez/ai-guard-claude-code-hooks`). `[tool.hatch.metadata] allow-direct-references = true` opts hatchling into that form. Both `pip install git+…` and `uv sync` resolve to the same branch. Don't replace with the upstream release until `ddtrace.appsec.ai_guard` lands in a published version.
- Every source/config/script file carries the standard Datadog Apache-2.0 header (see [CONTRIBUTING.md](CONTRIBUTING.md#file-header) for the exact wording). Use the comment syntax for the file's language; skip docs, JSON, and test fixtures.

## Important constraints

- **The hook CLI never exits non-zero.** Any failure (unknown agent, missing credentials, handler exception, malformed payload) is logged via `logger.exception("failed to invoke hook …")` and the command exits 0 — a broken hook must never break the user's coding session.
- **Hook stdout is the decision channel.** Only the JSON the host agent expects may be written to stdout; everything else goes to the log file.
- **`config.env` is loaded once, by the CLI.** `cli.main` calls `storage.load_into_environ()` before dispatching, so subcommands (and the hooks) read `DD_*` from `os.environ`. Don't re-read `config.env` inside individual commands.
- **The legacy proxy-service teardown is silent.** Don't surface it in install/uninstall output — users who never ran the old proxy shouldn't be told about it.

## Running locally with Docker Compose

The `claude` container runs Claude Code with the ai-guard hooks pre-wired into `~/.claude/settings.json`; they evaluate in-process, so there's nothing else to start. A `mitmproxy` sidecar is an optional debugging HTTPS proxy (`HTTPS_PROXY`) that lets you watch Claude's calls to `api.anthropic.com` and ai-guard's AI Guard `/api/v2/ai-guard/evaluate` requests — it is not part of ai-guard.

```bash
cp .env.example .env       # set DD_API_KEY, DD_APP_KEY (the hooks read these from the container env)
docker compose build
docker compose up -d
docker exec -ti ai-guard-coding-agents-claude-1 claude
```

mitmproxy UI: `http://localhost:8081` (password: `ai_guard`). The hook's log lands in `docker/.ai_guard/ai-guard.log` — the container's `$XDG_STATE_HOME/ai-guard` is bind-mounted there.
