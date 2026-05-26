# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, Cursor, …) working in this repository.

## Purpose

Real-time guardrails for coding agents. The CLI ships two pieces that work together:

1. **A long-running HTTP proxy** (`ai-guard proxy`) that sits between the agent and the upstream LLM API. It captures every request/response, persists the conversation, and exposes a `/hook/<agent>/<hook>` endpoint that the agent's hook runner can POST events to.
2. **A short-lived hook shim** (`ai-guard hook AGENT HOOK`) that reads a JSON event from stdin, POSTs it to the running proxy, and writes any response back to stdout. Wired into Claude Code via `~/.claude/settings.json`.

Both flows ultimately land in a single `ProxyHandler` (`ClaudeProxy`) that does the actual evaluation against Datadog AI Guard, span emission, and per-session storage.

## Repository layout

```
/
├── src/aiguard/
│   ├── cli.py                      # Top-level Click CLI: `proxy`, `hook` commands
│   ├── constants.py                # AIGuardConstants — span names + tag keys
│   ├── storage.py                  # Per-session JSON persistence under $XDG_STATE_HOME/ai-guard/<agent>/<sid>/<slot>.json (slot is `main` for the parent session or the subagent's agent_id)
│   ├── hooks/
│   │   └── hooks.py                # `ai-guard hook` HTTP shim (aiohttp client → proxy)
│   ├── proxy/
│   │   ├── server.py               # Proxy + ProxyHandler ABC + URL routing for /hook/* and upstream
│   │   └── util.py                 # WHATWG SSE parser
│   └── claude/
│       ├── proxy.py                # ClaudeProxy: handle_hook + parse_request/response + helpers
│       └── util.py                 # fetch_user_id() — reads ~/.claude.json
├── tests/
│   ├── conftest.py                 # Shared fixtures (tmp_home, storage_root, proxy_client, fake_ai_guard, tracer_recorder)
│   ├── fixtures/                   # Canned Anthropic JSON + SSE bodies, sample ~/.claude.json
│   ├── unit/{test_claude, test_cli, test_storage}.py
│   └── integration/{test_proxy, test_binary_proxy}.py
├── docker/claude/
│   ├── Dockerfile                  # Multi-stage: PyInstaller binary + Claude Code runtime
│   ├── claude-settings.json        # Wires every Claude Code hook to `ai-guard hook claude <Event>`
│   └── entrypoint.sh               # Starts the proxy, waits for readiness, execs CMD
├── .github/workflows/
│   ├── test.yml                    # Cross-platform (linux/macos/windows × x86_64/arm64) test + binary smoke
│   └── build.yml                   # Tagged release build (calls test.yml first)
├── docker-compose.yml              # mitmproxy + claude services for local testing
├── ai-guard.spec                   # PyInstaller spec — single-file binary
├── pyproject.toml                  # uv project metadata, optional `test`/`build` extras
├── .github/{PULL_REQUEST_TEMPLATE,ISSUE_TEMPLATE/*}.md  # OSS templates
├── CONTRIBUTING.md                 # PR workflow, dev setup, header convention
├── LICENSE                         # Apache 2.0
├── LICENSE-3rdparty.csv            # Direct deps tracked for OSS compliance
└── NOTICE                          # Apache 2.0 attribution
```

## Key entry points

| Command | Source | What it does |
|---|---|---|
| `ai-guard proxy` | `proxy/server.py:proxy` | Start the long-running aiohttp proxy. Each registered handler declares its own upstream (`ClaudeProxy.upstream()`); requests are routed to handlers via `matches()`, hook events via the `ai-guard-cli` UA. Flags: `--host` (`DD_AI_GUARD_PROXY_HOST`, default `127.0.0.1`), `--port` (`DD_AI_GUARD_PROXY_PORT`, default `29279`), `--anthropic-upstream` (`DD_AI_GUARD_ANTHROPIC_UPSTREAM`, default `https://api.anthropic.com`), `--block/--no-block` (`DD_AI_GUARD_BLOCK`). |
| `ai-guard hook <AGENT> <HOOK>` | `hooks/hooks.py:hook` | One-shot CLI: read stdin, POST to `<PROXY_URL>/hook/<agent>/<hook>` with `User-Agent: ai-guard-cli/<version>`, write reply to stdout. Flag: `--proxy-url` (`DD_AI_GUARD_PROXY_URL`, default `http://127.0.0.1:29279`). **Always exits 0** so a failed hook never breaks the host agent's command flow. |

The CLI is registered in `src/aiguard/cli.py` via `main.add_command(proxy)` and `main.add_command(hook)` (the `hook` Click command is imported from `aiguard.hooks.hooks`).

## How a hook flows end-to-end

```
Claude Code hook subprocess
   │   stdin = event JSON
   ▼
ai-guard hook claude SessionStart        # hooks/hooks.py
   │   POST /hook/claude/SessionStart
   │   User-Agent: ai-guard-cli/<version>     ← routing key for the proxy
   ▼
Proxy._handle               # proxy/server.py
   │   _is_hook_request(request) → True (UA contains "ai-guard-cli")
   ▼
Proxy._handle_hook
   │   parses path → (agent="claude", hook="SessionStart")
   │   looks up handler by agent()
   ▼
ClaudeProxy.handle_hook(hook, payload)   # claude/proxy.py
   │   maps "SessionStart" → method "_session_start"
   │   awaits self._<snake_case_hook>(event)
   ▼
_session_start(event)  →  emits span via @tracer.wrap, sets tags, returns dict|None
```

Dispatch is dynamic: `getattr(self, "_" + camel_to_snake(hook), None)`. To add a hook, add a method named `_<snake_case>` and the proxy will route to it.

## How the proxy + storage work

`Proxy._handle` routes by request `User-Agent`:

- **`User-Agent` contains `ai-guard-cli`** → `_handle_hook`. The path must be `/hook/<agent>/<hook>`; otherwise 404.
- **Anything else** → `_handle_proxy`. The first handler whose `matches(request)` returns true gets it; the request body is run through `handler.parse_request(...) → (session_id, agent_id, messages)` and persisted via `storage.save_messages(..., agent_id=...)` so subagent traffic lands in its own slot. The request is forwarded to **`handler.upstream()`** (each handler owns its upstream), and after the response streams through, response messages parsed by `handler.parse_response(...)` are appended via load + save.
- **No handler claims the request** → `502 Bad Gateway` with `text="no handler claims <method> <path>"`. The proxy is intentionally per-agent; there is no global passthrough.

`matches()` is User-Agent-only by convention (`ClaudeProxy.matches` checks for `claude-cli`); method/path/content-type validation belongs in `parse_request`, which returns `("", "", [])` to skip persistence without rejecting the request.

Storage layout is `$XDG_STATE_HOME/ai-guard/<agent>/<session_id>/<slot>.json` (overridable with `DD_AI_GUARD_HOME`). The slot is `main` for the parent session and the subagent's `agent_id` for sidechain calls, so a Task-spawned subagent that shares the Claude `session_id` gets its own history file instead of overwriting the parent's. The session/agent identifiers come from the `X-Claude-Code-Session-Id` and `X-Claude-Code-Agent-Id` headers that Claude Code stamps on every Anthropic request (the agent header is absent for the parent session). `storage._session_file` resolves the candidate path and runs a `relative_to(root)` containment check, so a hostile `agent`/`session_id`/`agent_id` that escapes the storage tree short-circuits to a no-op (load returns `[]`, save/delete log + bail). `SessionEnd` deletes the whole `<session_id>/` directory — every subagent slot included.

The proxy does **not** evaluate during proxying — it only persists. AI Guard evaluation happens inside the hook handlers (`_pre_tool_use` / `_post_tool_use` / `_post_tool_use_failure`), which load the persisted history, append the new tool exchange, and call `self._ai_guard.evaluate(...)`.

## Currently wired hooks (Claude)

Method names map 1:1 to the event names in `docker/claude/claude-settings.json`:

| Hook event | Method | What it does |
|---|---|---|
| `SessionStart` | `_session_start` | Emits a span with session_id, model, user email tags |
| `SessionEnd` | `_session_end` | Emits a span; **deletes** the session's stored conversation |
| `SubagentStart` | `_subagent_start` | Emits a span with subagent_id / subagent_type tags |
| `SubagentStop` | `_subagent_stop` | Emits a span with subagent_id / subagent_type tags |
| `PreToolUse` | `_pre_tool_use` | Loads stored history; for `Skill` tool calls injects the resolved `SKILL.md` body into the tool message so AI Guard sees what is about to be loaded. On `AIGuardAbortError` returns `{"hookSpecificOutput": {"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":...,"additionalContext":...}}` — `permissionDecisionReason` is the branded TUI banner, `additionalContext` is the structured guidance for Claude on how to message the user. |
| `PostToolUse` | `_post_tool_use` | Appends the tool result to the stored history and re-evaluates. On abort returns both `{"decision":"block","reason":...}` (TUI banner) and `hookSpecificOutput.additionalContext` with the same user-messaging guidance. |
| `PostToolUseFailure` | `_post_tool_use_failure` | Same shape as PostToolUse but uses `event.error` as the tool content. |

## Adding a new Claude Code hook

1. Add a method `_<snake_case_hook_name>` on `ClaudeProxy`. Decorate with `@tracer.wrap(name=..., resource=AIGuardConstants.HOOK_RESOURCE)` if you want a span. The signature is `async def _foo(self, event: dict[str, Any]) -> dict[str, Any] | None`.
2. Register the hook in `docker/claude/claude-settings.json` so Claude Code invokes `ai-guard hook claude <CamelCaseName>` on the right lifecycle event.
3. Add a test in `tests/unit/test_claude.py` under `TestHandleHookSpans` or `TestHandleHookToolUse` (depending on the shape).

If the hook needs new span tags or operation names, add constants in `src/aiguard/constants.py`.

## Adding a new agent (e.g., Codex, Cursor)

1. Create `src/aiguard/<agent>/proxy.py` with a class that implements `ProxyHandler`'s six abstract methods:
   - `agent()` — lowercase name used in `/hook/<agent>/...` URLs and CLI invocations.
   - `upstream()` — the LLM API base URL the proxy will forward to when this handler matches.
   - `matches(request)` — return true when this handler claims the request (User-Agent check by convention).
   - `parse_request(request, body) -> (session_id, agent_id, messages)` — `agent_id` is empty for the parent session and the subagent's identifier when present, so storage can keep sidechain history in its own slot. Return `("", "", [])` to skip persistence.
   - `parse_response(response, body) -> messages`.
   - `handle_hook(hook, payload) -> bytes` (async).
2. Register the handler in `proxy/server.py:proxy()` alongside `ClaudeProxy`. Add a `--<agent>-upstream` Click option (`DD_AI_GUARD_<AGENT>_UPSTREAM` envvar) with a sensible default.
3. Add hidden imports for the new module to `ai-guard.spec`.
4. Add a wiring file under `docker/<agent>/` analogous to `claude-settings.json`.

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
- All hook handler methods are `async`, return `dict | None`, and are decorated with `@tracer.wrap`. Inside, set tags via `tracer.current_span()`, not by manually opening a span.
- Storage is the **source of truth** for conversation history during a session; the proxy persists, the hooks load. Don't keep parallel in-memory copies.
- Logging goes to `$XDG_STATE_HOME/ai-guard/ai-guard.log` (rotating, 1 MB × 10 backups). Hook stdout must contain **only** the JSON decision the host agent expects — no extra prints.
- The `ddtrace` dependency is pinned to a custom branch via a direct reference in `project.dependencies` (`ddtrace @ git+https://github.com/DataDog/dd-trace-py@malvarez/ai-guard-claude-code-hooks`). `[tool.hatch.metadata] allow-direct-references = true` opts hatchling into that form. Both `pip install git+...` and `uv sync` resolve to the same branch. Don't replace with the upstream release until `ddtrace.appsec.ai_guard` lands in a published version.
- Every source/config/script file carries the standard Datadog Apache-2.0 header (see [CONTRIBUTING.md](CONTRIBUTING.md#file-header) for the exact wording). Use the comment syntax for the file's language; skip docs, JSON, and test fixtures.

## Important constraints

- **Hook CLI never exits non-zero.** A failed POST is logged via `logger.exception("failed to invoke hook")` and the command exits 0. This is intentional — a broken proxy must not break the user's coding session.
- **The hook CLI's `User-Agent: ai-guard-cli/<version>` is load-bearing.** The proxy uses that substring to decide between `_handle_hook` and `_handle_proxy`. Any client that hits `/hook/...` directly (curl, scripts) must include the same UA or the request goes through the proxy path and fails.
- **Per-agent upstreams, no global fallback.** Unmatched requests get 502 — the proxy is not a generic transparent forwarder. Every handler must declare its own `upstream()`.
- **Proxy bound to localhost by default.** `--host` defaults to `127.0.0.1`. Pass `0.0.0.0` (or set `DD_AI_GUARD_PROXY_HOST=0.0.0.0`, as the docker-compose service does) only when something off-box must reach it.
- **Path traversal is hardened in `storage.py`.** Any change that builds a file path from user-controlled input must go through `storage._session_file` (which resolves the candidate and `relative_to(root)`-checks containment), not raw concatenation.
- **The proxy must preserve Anthropic response headers exactly** (especially `content-type: text/event-stream`) so Claude Code's SSE parser doesn't break. `_resp_headers` strips only hop-by-hop headers + `content-encoding`.

## Running locally with Docker Compose

```bash
cp .env.example .env       # set DD_API_KEY, DD_APP_KEY
docker compose build
docker compose up -d
docker exec -ti ai-guard-coding-agents-claude-1 claude
```

mitmproxy UI: `http://localhost:8081` (password: `ai_guard`). Claude's traffic and AI Guard `/api/v2/ai-guard/evaluate` calls are all visible there.