# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Generic AI Guard HTTP proxy server."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

import aiohttp
import aiohttp.web
import click
from ddtrace.appsec.ai_guard import Message

from aiguard.storage import save_messages

logger = logging.getLogger("ai_guard")

_SKIP_REQ_HEADERS = frozenset({"host", "content-length", "transfer-encoding"})
_SKIP_RESP_HEADERS = frozenset({"content-length", "transfer-encoding", "connection"})

# Connection pool
_POOL_TOTAL_LIMIT = 500
_POOL_PER_HOST_LIMIT = 100
_POOL_DNS_TTL = 300  # seconds
_POOL_KEEPALIVE_TIMEOUT = 60  # seconds
_POOL_CONNECT_TIMEOUT = 10  # seconds


class ProxyHandler(ABC):
    """Parses messages for a specific upstream API endpoint."""

    @abstractmethod
    def agent(self) -> str:
        """Return the name of the agent."""

    @abstractmethod
    def upstream(self) -> str:
        """Return the upstream endpoint."""

    @abstractmethod
    def matches(self, request: aiohttp.web.Request) -> bool:
        """Return True if this handler should process the given request."""

    @abstractmethod
    def parse_request(self, request: aiohttp.web.Request, body: bytes) -> tuple[str, list[Message]]:
        """Extract session_id and AI Guard messages from a request body. Return
        ``("", [])`` for requests this handler doesn't want to persist."""

    @abstractmethod
    def parse_response(self, response: aiohttp.ClientResponse, body: bytes) -> list[Message]:
        """Extract AI Guard messages from a response body."""

    @abstractmethod
    async def handle_hook(self, hook: str, payload: bytes) -> bytes:
        """Dispatch the named hook event; return the agent-shaped response body."""


class Proxy:
    """HTTP proxy that evaluates AI Guard using a list of ProxyHandlers."""

    def __init__(
        self,
        host: str,
        port: int,
        handlers: list[ProxyHandler],
    ) -> None:
        self._host = host
        self._port = port
        self._handlers = handlers
        self._session: aiohttp.ClientSession | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @staticmethod
    def _open_session() -> aiohttp.ClientSession:
        connector = aiohttp.TCPConnector(
            limit=_POOL_TOTAL_LIMIT,
            limit_per_host=_POOL_PER_HOST_LIMIT,
            ttl_dns_cache=_POOL_DNS_TTL,
            use_dns_cache=True,
            keepalive_timeout=_POOL_KEEPALIVE_TIMEOUT,
        )
        return aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(
                total=None,
                connect=_POOL_CONNECT_TIMEOUT,
                sock_connect=_POOL_CONNECT_TIMEOUT,
            ),
            auto_decompress=True,
            trust_env=True,
        )

    def build_app(self) -> aiohttp.web.Application:
        """Build the aiohttp Application. Tests mount this on a TestServer."""
        if self._session is None:
            self._session = self._open_session()
        app = aiohttp.web.Application(client_max_size=0)
        app.router.add_route("*", "/{path_info:.*}", self._handle)

        async def _close_session(_app: aiohttp.web.Application) -> None:
            if self._session is not None:
                await self._session.close()

        app.on_cleanup.append(_close_session)
        return app

    async def run(self) -> None:
        app = self.build_app()
        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, self._host, self._port)
        await site.start()
        logger.info(
            "proxy listening %s:%d (handlers: %s)",
            self._host,
            self._port,
            ", ".join(f"{h.agent()}->{h.upstream()}" for h in self._handlers) or "<none>",
        )
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    # ── Request handling ──────────────────────────────────────────────────────

    async def _handle(self, request: aiohttp.web.Request) -> aiohttp.web.StreamResponse:
        path = "/" + request.match_info.get("path_info", "")
        if request.query_string:
            path = f"{path}?{request.query_string}"

        try:
            return await (
                self._handle_hook(request, path)
                if _is_hook_request(request)
                else self._handle_proxy(request, path)
            )
        except aiohttp.ClientError:
            logger.error("upstream error %s %s", request.method, path, exc_info=True)
            raise
        except Exception:
            logger.exception("unhandled error %s %s", request.method, path, exc_info=True)
            raise

    async def _handle_hook(
        self, request: aiohttp.web.Request, path: str
    ) -> aiohttp.web.StreamResponse:
        """Dispatch a hook event to the matching handler.

        Path format: ``/hook/<agent>/<hook>``. The body is the raw hook event
        payload (JSON for Claude Code hooks); the response body is whatever the
        handler emits back to the calling agent.
        """
        parts = path.split("?", 1)[0].strip("/").split("/")
        if len(parts) < 3 or parts[0] != "hook":
            return aiohttp.web.Response(status=404, text="hook path must be /hook/<agent>/<hook>")
        agent = parts[1]
        # Allow nested hook names ("foo/bar") to pass through verbatim.
        hook = "/".join(parts[2:])

        handler = next((h for h in self._handlers if h.agent() == agent), None)
        if handler is None:
            return aiohttp.web.Response(status=404, text=f"no handler for agent {agent!r}")

        logger.debug("hook received for agent %s and hook %s", agent, hook)
        req_body = await request.read()
        try:
            response = await handler.handle_hook(hook, req_body)
        except Exception:
            logger.exception("hook failed: agent=%s hook=%s", agent, hook)
            return aiohttp.web.Response(status=500)

        if not response:
            return aiohttp.web.Response(status=204)
        return aiohttp.web.Response(status=200, body=response, content_type="application/json")

    async def _handle_proxy(
        self, request: aiohttp.web.Request, path: str
    ) -> aiohttp.web.StreamResponse:
        handler = next((h for h in self._handlers if h.matches(request)), None)
        if handler is None:
            return aiohttp.web.Response(
                status=502, text=f"no handler claims {request.method} {path}"
            )

        req_headers = {
            k: v for k, v in request.headers.items() if k.lower() not in _SKIP_REQ_HEADERS
        }
        req_body = await request.read()
        logger.debug("handler %s ready to handle %s %s", handler.agent(), request.method, path)
        req_messages = []
        session_id = ""
        try:
            session_id, req_messages = handler.parse_request(request, req_body)
            if session_id and req_messages:
                save_messages(handler.agent(), session_id, req_messages)
        except Exception:
            logger.exception("failed to extract request messages")

        async with self._session.request(
            request.method,
            f"{handler.upstream().rstrip('/')}{path}",
            headers=req_headers,
            data=req_body,
        ) as upstream:
            resp = aiohttp.web.StreamResponse(
                status=upstream.status, headers=_response_headers(upstream)
            )
            await resp.prepare(request)
            chunks: list[bytes] = []
            async for chunk in upstream.content.iter_any():
                await resp.write(chunk)
                chunks.append(chunk)

            if session_id and req_messages:
                try:
                    resp_messages = handler.parse_response(upstream, b"".join(chunks))
                    if resp_messages:
                        agent = handler.agent()
                        save_messages(
                            agent,
                            session_id,
                            req_messages + resp_messages,
                        )
                except Exception:
                    logger.exception("failed to extract response messages", exc_info=True)

            await resp.write_eof()
            return resp


# ── Helpers ───────────────────────────────────────────────────────────────────


def _response_headers(up: aiohttp.ClientResponse) -> dict[str, str]:
    skip = _SKIP_RESP_HEADERS | {"content-encoding"}
    return {k: v for k, v in up.headers.items() if k.lower() not in skip}


def _is_hook_request(request: aiohttp.web.Request) -> bool:
    user_agent = request.headers.get("User-Agent", "")
    return "ai-guard-cli" in user_agent


# ── CLI group ─────────────────────────────────────────────────────────────────


@click.command("proxy")
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    envvar="DD_AI_GUARD_PROXY_HOST",
    help="Local interface to bind. Use 0.0.0.0 to expose on all interfaces.",
)
@click.option(
    "--port",
    default=29279,
    show_default=True,
    type=int,
    envvar="DD_AI_GUARD_PROXY_PORT",
    help="Local port to listen on.",
)
@click.option(
    "--anthropic-upstream",
    default="https://api.anthropic.com",
    show_default=True,
    envvar="DD_AI_GUARD_ANTHROPIC_UPSTREAM",
    help="Anthropic API base URL (used by the Claude handler).",
)
@click.option(
    "--block/--no-block",
    default=True,
    show_default=True,
    envvar="DD_AI_GUARD_BLOCK",
    help="Block requests/responses when AI Guard flags them.",
)
def proxy(host: str, port: int, anthropic_upstream: str, block: bool) -> None:
    """Transparent HTTP proxy that inspects LLM traffic in real time.

    Sits between the agent and the upstream API, evaluating every
    request and response through Datadog AI Guard. Unsafe payloads
    are blocked before they reach the model or the caller.

    \b
    Examples:
      ai-guard proxy
      ai-guard proxy --port 9000 --no-block
      ai-guard proxy --host 0.0.0.0 --anthropic-upstream https://api.anthropic.com
    """

    from aiguard.claude.proxy import ClaudeProxy

    handlers = [ClaudeProxy(anthropic_upstream, block)]
    asyncio.run(Proxy(host=host, port=port, handlers=handlers).run())
