"""Binary integration: drive real HTTP requests through the built ai-guard proxy.

Skipped unless the PyInstaller binary is present at ``dist/ai-guard[.exe]``
(or at the path in ``AI_GUARD_BINARY``). CI runs these from the smoke job on
every OS/arch after building the binary.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import pytest

from aiguard import storage

REPO_ROOT = Path(__file__).resolve().parents[2]
BINARY = (
    Path(os.environ["AI_GUARD_BINARY"])
    if os.environ.get("AI_GUARD_BINARY")
    else REPO_ROOT / "dist" / ("ai-guard.exe" if sys.platform == "win32" else "ai-guard")
)

pytestmark = pytest.mark.binary

if not BINARY.exists():
    pytest.skip(
        f"ai-guard binary not found at {BINARY} — build it with `pyinstaller ai-guard.spec` "
        "or set AI_GUARD_BINARY to skip this hint",
        allow_module_level=True,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    try:
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_for_port(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"port {port} did not open within {timeout:.0f}s")


def _start_thread_server(handler_cls: type[BaseHTTPRequestHandler]) -> tuple[HTTPServer, Thread]:
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, thread


def _stop_server(srv: HTTPServer, thread: Thread) -> None:
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _make_anthropic_handler(
    state: dict[str, Any], response_body: bytes
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            state["calls"].append({"path": self.path, "body": body})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, fmt: str, *args: Any) -> None:
            return None

    return Handler


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def anthropic_mock() -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"calls": [], "response": b""}

    response_body = json.dumps(
        {
            "id": "msg_binary_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-5",
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": [{"type": "text", "text": "ok from upstream"}],
        }
    ).encode()
    state["response"] = response_body

    handler = _make_anthropic_handler(state, response_body)
    srv, thread = _start_thread_server(handler)
    state["url"] = f"http://127.0.0.1:{srv.server_port}"
    try:
        yield state
    finally:
        _stop_server(srv, thread)


@pytest.fixture
def proxy_process(
    anthropic_mock: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, Any]]:
    port = _free_port()
    log_file = tmp_path / "ai_guard.log"
    home = tmp_path / "home"
    home.mkdir()

    # The test process and the binary subprocess both need to look at the same
    # storage root so we can read what the binary wrote.
    monkeypatch.setenv("DD_AI_GUARD_HOME", str(home))

    env = {
        **os.environ,
        "DD_AI_GUARD_PROXY_PORT": str(port),
        "DD_AI_GUARD_ANTHROPIC_UPSTREAM": anthropic_mock["url"],
        "DD_AI_GUARD_LOG_FILE": str(log_file),
        "DD_AI_GUARD_HOME": str(home),
        "DD_API_KEY": os.environ.get("DD_API_KEY", "test-api-key"),
        "DD_APP_KEY": os.environ.get("DD_APP_KEY", "test-app-key"),
        # Quiet ddtrace internals so the binary doesn't try to reach a real agent.
        "DD_TRACE_ENABLED": "false",
        "DD_INSTRUMENTATION_TELEMETRY_ENABLED": "false",
        "DD_REMOTE_CONFIGURATION_ENABLED": "false",
    }

    popen_kwargs: dict[str, Any] = {
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen([str(BINARY), "proxy"], **popen_kwargs)
    try:
        try:
            _wait_for_port(port, timeout=30)
        except TimeoutError:
            proc.terminate()
            stdout, stderr = proc.communicate(timeout=5)
            raise AssertionError(
                f"binary proxy did not bind port {port}.\n"
                f"stdout:\n{stdout.decode(errors='replace')}\n"
                f"stderr:\n{stderr.decode(errors='replace')}"
            ) from None

        yield {"port": port, "proc": proc, "home": home}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _post_messages(port: int, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    payload = json.dumps(body).encode()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        conn.request(
            "POST",
            "/v1/messages",
            payload,
            {
                "Content-Type": "application/json",
                "User-Agent": "claude-cli/1.0.0 (External, cli)",
            },
        )
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, json.loads(data) if data else {}
    finally:
        conn.close()


def _wait_for_storage(home: Path, agent: str, session_id: str, timeout: float = 5.0) -> Path:
    path = home / agent / f"{session_id}.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not path.exists():
        time.sleep(0.05)
    return path


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_binary_passthrough_persists_messages(
    proxy_process: dict[str, Any],
    anthropic_mock: dict[str, Any],
) -> None:
    session_id = "binary-sess-1"
    user_id_payload = json.dumps({"session_id": session_id, "account_uuid": "acct-1"})

    status, body = _post_messages(
        proxy_process["port"],
        {
            "model": "claude-sonnet-4-5",
            "metadata": {"user_id": user_id_payload},
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert status == 200
    assert body["id"] == "msg_binary_test"

    # Upstream Anthropic was called once on the right path.
    assert [c["path"] for c in anthropic_mock["calls"]] == ["/v1/messages"]

    # The binary persisted the conversation under ~/.ai_guard/claude/<session>.json.
    path = _wait_for_storage(proxy_process["home"], "claude", session_id)
    assert path.exists(), f"expected session file at {path}"

    stored = storage.load_messages("claude", session_id)
    roles = [m.get("role") for m in stored]
    assert roles[0] == "user"
    assert roles[-1] == "assistant"
    # The assistant text from the upstream JSON response is in the file.
    last = stored[-1]
    assert any(p.get("text") == "ok from upstream" for p in last.get("content", []))


def test_binary_skips_storage_when_session_id_missing(
    proxy_process: dict[str, Any],
    anthropic_mock: dict[str, Any],
) -> None:
    # No metadata.user_id → no session id → nothing to write.
    status, _ = _post_messages(
        proxy_process["port"],
        {
            "model": "claude-sonnet-4-5",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert status == 200
    assert anthropic_mock["calls"], "upstream should still have been called"

    # Allow time for any (non-)write to settle.
    time.sleep(0.2)
    home = proxy_process["home"]
    json_files = list(home.rglob("*.json")) if home.exists() else []
    assert json_files == []
