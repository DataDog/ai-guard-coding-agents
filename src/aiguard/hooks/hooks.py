# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""``ai-guard hook AGENT HOOK`` — thin HTTP shim to the running proxy."""

from __future__ import annotations

import asyncio
import logging
import sys
import urllib.parse

import aiohttp
import click

from aiguard import __version__

logger = logging.getLogger("ai_guard")

# The proxy's _is_hook_request() routes on this UA; keep the substring stable.
_USER_AGENT = f"ai-guard-cli/{__version__}"


async def _post(url: str, payload: bytes) -> tuple[int, bytes]:
    """POST ``payload`` to ``url`` and return ``(status, body)``."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": _USER_AGENT,
            },
        ) as resp:
            return resp.status, await resp.read()


@click.command("hook")
@click.argument("agent")
@click.argument("hook_name", metavar="HOOK")
@click.option(
    "--proxy-url",
    "proxy_url",
    envvar="DD_AI_GUARD_PROXY_URL",
    default="http://127.0.0.1:29279",
    show_default=True,
    help="Base URL of the running ai-guard proxy.",
)
def hook(agent: str, hook_name: str, proxy_url: str) -> None:
    """Forward a hook event to the running ai-guard proxy.

    Reads the event payload from stdin, POSTs it to
    ``<PROXY_URL>/hook/<AGENT>/<HOOK>``, and writes any response body to
    stdout. Exits 1 if the proxy is unreachable or returns an error status.

    \b
    Examples:
      ai-guard hook claude SessionStart < event.json
      ai-guard hook claude SubagentStop < event.json
    """
    try:
        payload = sys.stdin.buffer.read()
        url = proxy_url.rstrip("/") + f"/hook/{agent}/{hook_name}"

        parsed = urllib.parse.urlparse(url)
        if not parsed.scheme or not parsed.hostname:
            raise click.ClickException(f"invalid proxy URL: {proxy_url!r}")

        try:
            status, body = asyncio.run(_post(url, payload))
        except aiohttp.InvalidURL as exc:
            raise click.ClickException(f"invalid proxy URL: {proxy_url!r}") from exc
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise click.ClickException(
                f"failed to reach ai-guard proxy at {proxy_url}: {exc}"
            ) from exc

        if status >= 400:
            detail = body.decode("utf-8", errors="replace").strip() or f"status {status}"
            raise click.ClickException(f"proxy returned {status}: {detail}")

        if body:
            sys.stdout.buffer.write(body)
    except Exception:
        # Swallowed by design: a failed hook must not break the host agent's
        # command flow. The error is logged with traceback for diagnosis.
        logger.exception("failed to invoke hook", exc_info=True)
