# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Generic AI Guard HTTP proxy server."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import aiohttp
import aiohttp.web
import click

from aiguard import utils
from aiguard.constants import AIGuardConstants
from aiguard.storage import save_messages

if TYPE_CHECKING:
    # Annotation-only (``from __future__ import annotations`` above). Kept off
    # the runtime import path so importing this module doesn't pull in ddtrace
    # — the proxy command imports ddtrace lazily, after loading DD_API_KEY from
    # the keychain and scrubbing OTEL_ vars.
    from ddtrace.appsec.ai_guard import Message

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
    def parse_request(
        self, request: aiohttp.web.Request, body: bytes
    ) -> tuple[str, str, list[Message]]:
        """Extract session_id, agent_id, and AI Guard messages from a request body.

        ``agent_id`` is empty for the parent session and the subagent's
        identifier for sidechain calls — storage keys per-slot so subagent and
        main-session histories don't overwrite each other. Return
        ``("", "", [])`` for requests this handler doesn't want to persist."""

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
        idle_timeout: float = 0.0,
    ) -> None:
        self._host = host
        self._port = port
        self._handlers = handlers
        self._session: aiohttp.ClientSession | None = None
        self._idle_timeout = idle_timeout
        self._last_activity = time.monotonic()
        self._active_requests = 0

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

    @aiohttp.web.middleware
    async def _track_activity(
        self,
        request: aiohttp.web.Request,
        handler,
    ) -> aiohttp.web.StreamResponse:
        self._last_activity = time.monotonic()
        self._active_requests += 1
        try:
            return await handler(request)
        finally:
            self._active_requests -= 1
            self._last_activity = time.monotonic()

    def build_app(self) -> aiohttp.web.Application:
        """Build the aiohttp Application. Tests mount this on a TestServer."""
        if self._session is None:
            self._session = self._open_session()
        app = aiohttp.web.Application(
            client_max_size=0,
            middlewares=[self._track_activity],
        )
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
        sites = await self._start_sites(runner)
        bind_summary = ", ".join(_describe_site(s) for s in sites)
        logger.info(
            "proxy listening %s (handlers: %s)",
            bind_summary,
            ", ".join(f"{h.agent()}->{h.upstream()}" for h in self._handlers) or "<none>",
        )
        try:
            await self._serve_until_idle()
        finally:
            await runner.cleanup()

    async def _start_sites(self, runner: aiohttp.web.AppRunner) -> list[aiohttp.web.BaseSite]:
        """Use sockets handed in by launchd / systemd if any, else bind ourselves."""
        sites: list[aiohttp.web.BaseSite] = []
        for sock in _inherited_sockets():
            sock.setblocking(False)
            site = aiohttp.web.SockSite(runner, sock)
            await site.start()
            sites.append(site)
        if sites:
            return sites
        site = aiohttp.web.TCPSite(runner, self._host, self._port)
        await site.start()
        return [site]

    async def _serve_until_idle(self) -> None:
        """Block forever, or until ``idle_timeout`` seconds pass with no traffic."""
        if self._idle_timeout <= 0:
            await asyncio.Event().wait()
            return
        # Poll often enough to react within ~10% of the configured timeout, but
        # never busy-loop (≥1s) and never hold up shutdown for more than a
        # minute on long timeouts.
        check_interval = max(1.0, min(self._idle_timeout / 10.0, 60.0))
        while True:
            await asyncio.sleep(check_interval)
            if self._active_requests > 0:
                continue
            idle = time.monotonic() - self._last_activity
            if idle >= self._idle_timeout:
                logger.info(
                    "idle for %.0fs (>= %.0fs), shutting down",
                    idle,
                    self._idle_timeout,
                )
                return

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
        agent_id = ""
        try:
            session_id, agent_id, req_messages = handler.parse_request(request, req_body)
            if session_id and req_messages:
                save_messages(handler.agent(), session_id, req_messages, agent_id=agent_id)
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
                        save_messages(
                            handler.agent(),
                            session_id,
                            req_messages + resp_messages,
                            agent_id=agent_id,
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


def _describe_site(site: aiohttp.web.BaseSite) -> str:
    try:
        return site.name
    except NotImplementedError:
        return type(site).__name__


def _inherited_sockets() -> list[socket.socket]:
    """Return listening sockets handed to us by the init system, if any.

    systemd uses the ``LISTEN_FDS`` protocol — file descriptors 3..3+N are
    pre-bound listening sockets when ``LISTEN_PID`` matches our pid. launchd
    on macOS hands sockets back through ``launch_activate_socket`` in
    libSystem; we look up the ``"Listener"`` entry from the plist.
    """
    if utils.is_linux():
        return _systemd_activate_socket()

    if utils.is_macos():
        return _launchd_activate_socket("Listener")

    return []


def _systemd_activate_socket() -> list[socket.socket]:
    if os.environ.get("LISTEN_PID") == str(os.getpid()):
        try:
            n = int(os.environ.get("LISTEN_FDS", "0"))
        except ValueError:
            n = 0
        return [socket.socket(fileno=fd) for fd in range(3, 3 + n)]
    else:
        return []


def _resolve_libsystem():
    """Resolve launch_activate_socket and free from libSystem (cached).

    Returns (launch_fn, free_fn) or (None, None) if anything is missing.
    """

    import ctypes.util

    libname = ctypes.util.find_library("System") or "/usr/lib/libSystem.B.dylib"
    try:
        libsys = ctypes.CDLL(libname)
    except Exception:
        logger.error("Could not load %s: %s", libname, exc_info=True)
        return None, None

    try:
        launch_fn = libsys.launch_activate_socket
    except AttributeError:
        logger.error(
            "launch_activate_socket missing from %s (requires macOS 10.10+)", libname, exc_info=True
        )
        return None, None

    launch_fn.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_int)),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    launch_fn.restype = ctypes.c_int

    free_fn = libsys.free
    free_fn.argtypes = [ctypes.c_void_p]
    free_fn.restype = None

    _launch_activate_socket, _free = launch_fn, free_fn
    return launch_fn, free_fn


def _launchd_activate_socket(name: str) -> list[socket.socket]:
    """Return listening sockets registered for ``name`` via launchd.

    Wraps ``launch_activate_socket(3)``. Returns ``[]`` when launchd has nothing to hand us:
    foreground run, not socket-activated, or the named entry isn't in the plist.
    """
    launch_fn, free_fn = _resolve_libsystem()
    if launch_fn is None:
        return []

    import ctypes
    import errno

    fds_ptr = ctypes.POINTER(ctypes.c_int)()
    count = ctypes.c_size_t(0)
    rc = launch_fn(name.encode("utf-8"), ctypes.byref(fds_ptr), ctypes.byref(count))

    if rc != 0:
        # ENOENT: name not present in the plist (common when not socket-activated).
        # ESRCH:  process isn't managed by launchd.
        # Anything else is suspicious enough to warn about.
        expected = rc in (errno.ENOENT, errno.ESRCH)
        logger.log(
            logging.DEBUG if expected else logging.WARNING,
            "launch_activate_socket(%r) failed: %s (errno %d)",
            name,
            os.strerror(rc),
            rc,
        )
        return []

    if not fds_ptr or count.value == 0:
        logger.debug("launch_activate_socket(%r) returned no sockets", name)
        return []

    # Copy the fd numbers out so we can free launchd's array immediately.
    raw_fds = [fds_ptr[i] for i in range(count.value)]
    try:
        free_fn(fds_ptr)
    except Exception:
        logger.exception("free() of launchd fd array failed; continuing", exc_info=True)

    socks: list[socket.socket] = []
    try:
        for fd in raw_fds:
            socks.append(socket.socket(fileno=fd))
    except OSError:
        failed_idx = len(socks)
        logger.exception(
            "Failed to wrap inherited fd %d as socket", raw_fds[failed_idx], exc_info=True
        )
        # Close sockets we already created; close raw fds we never reached
        # (including the one that just failed — socket() doesn't close on error).
        for s in socks:
            try:
                s.close()
            except OSError:
                pass
        for fd in raw_fds[failed_idx:]:
            try:
                os.close(fd)
            except OSError:
                pass
        raise

    return socks


# ── CLI group ─────────────────────────────────────────────────────────────────


@click.command("proxy")
@click.option(
    "--host",
    default=AIGuardConstants.PROXY_HOST_DEFAULT,
    show_default=True,
    envvar="DD_AI_GUARD_PROXY_HOST",
    help="Local interface to bind. Use 0.0.0.0 to expose on all interfaces.",
)
@click.option(
    "--port",
    default=AIGuardConstants.PROXY_PORT_DEFAULT,
    show_default=True,
    type=int,
    envvar="DD_AI_GUARD_PROXY_PORT",
    help="Local port to listen on.",
)
@click.option(
    "--anthropic-upstream",
    default=AIGuardConstants.ANTHROPIC_UPSTREAM_DEFAULT,
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
@click.option(
    "--idle-timeout",
    default=AIGuardConstants.PROXY_IDLE_TIMEOUT_DEFAULT,
    show_default=True,
    type=int,
    envvar="DD_AI_GUARD_PROXY_IDLE_TIMEOUT",
    help="Shut down after N seconds with no requests. 0 keeps the proxy "
    "running forever. Pair with socket activation (launchd/systemd) so the "
    "init system re-launches on demand.",
)
def proxy(host: str, port: int, anthropic_upstream: str, block: bool, idle_timeout: int) -> None:
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

    from aiguard import keychain

    keychain.load_into_env()

    for _key in [k for k in os.environ if k.startswith("OTEL_")]:
        del os.environ[_key]

    from ddtrace import tracer

    from aiguard.claude.proxy import ClaudeProxy

    handlers = [ClaudeProxy(anthropic_upstream, block)]
    try:
        asyncio.run(
            Proxy(
                host=host,
                port=port,
                handlers=handlers,
                idle_timeout=float(idle_timeout),
            ).run()
        )
    finally:
        # The proxy is the only command that emits spans; flush them before the
        # process exits (matters on the idle-timeout shutdown path).
        tracer.shutdown()
