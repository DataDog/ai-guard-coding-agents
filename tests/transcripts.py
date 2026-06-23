# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""Helpers for building Claude Code JSONL transcripts in tests.

Claude stores the main session at ``<project>/<session>.jsonl`` and each
subagent at ``<project>/<session>/subagents/agent-<agent_id>.jsonl``. Hook
payloads always carry the *main* transcript path, so the writers return it
regardless of which file they wrote — that mirrors what the handler sees.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def user_text(text: str) -> dict[str, Any]:
    """A plain user turn."""
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant_text(text: str) -> dict[str, Any]:
    """An assistant turn carrying a single text block."""
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def assistant_tool_use(tool_use_id: str, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """An assistant turn issuing one tool call."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}],
        },
    }


def tool_result(tool_use_id: str, content: Any, *, is_error: bool = False) -> dict[str, Any]:
    """A user turn carrying a tool result (Anthropic packs results into user turns)."""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                    "is_error": is_error,
                }
            ],
        },
    }


class TranscriptWriter:
    """Writes Claude Code JSONL transcripts under a fake projects directory."""

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir
        project_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _dump(entries: list[dict[str, Any]]) -> str:
        return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries)

    def main_path(self, session_id: str) -> str:
        return str(self.project_dir / f"{session_id}.jsonl")

    def write_main(self, session_id: str, entries: list[dict[str, Any]]) -> str:
        path = self.project_dir / f"{session_id}.jsonl"
        path.write_text(self._dump(entries), encoding="utf-8")
        return str(path)

    def write_subagent(self, session_id: str, agent_id: str, entries: list[dict[str, Any]]) -> str:
        sub_dir = self.project_dir / session_id / "subagents"
        sub_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / f"agent-{agent_id}.jsonl").write_text(self._dump(entries), encoding="utf-8")
        return self.main_path(session_id)

    def write_raw(self, session_id: str, text: str) -> str:
        """Write arbitrary bytes (for malformed-line / tolerance tests)."""
        path = self.project_dir / f"{session_id}.jsonl"
        path.write_text(text, encoding="utf-8")
        return str(path)


# ── Codex CLI rollout transcripts ──────────────────────────────────────────────
#
# Codex stores each session as a rollout JSONL file
# (``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl``). Every line is
# ``{"type", "payload"}`` and the conversation lives in ``response_item`` lines
# using the OpenAI Responses API item shape.


def codex_session_meta(session_id: str = "sess-1") -> dict[str, Any]:
    """A ``session_meta`` line (metadata; dropped by the translator)."""
    return {"type": "session_meta", "payload": {"id": session_id, "cli_version": "0.140.0"}}


def codex_user_message(text: str) -> dict[str, Any]:
    """A user ``message`` response item (``input_text`` content)."""
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def codex_assistant_message(text: str) -> dict[str, Any]:
    """An assistant ``message`` response item (``output_text`` content)."""
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def codex_developer_message(text: str) -> dict[str, Any]:
    """A developer ``message`` response item (Codex env/permission instructions)."""
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def codex_function_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    """A ``function_call`` response item. ``arguments`` is a JSON *string*."""
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": name,
            "arguments": arguments,
            "call_id": call_id,
        },
    }


def codex_function_call_output(call_id: str, output: str) -> dict[str, Any]:
    """A ``function_call_output`` response item."""
    return {
        "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": call_id, "output": output},
    }


def codex_custom_tool_call(call_id: str, name: str, tool_input: str) -> dict[str, Any]:
    """A ``custom_tool_call`` response item (e.g. ``apply_patch``).

    Unlike ``function_call``, the payload is a raw string in ``input`` (not a
    JSON ``arguments`` string).
    """
    return {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "status": "completed",
            "call_id": call_id,
            "name": name,
            "input": tool_input,
        },
    }


def codex_custom_tool_call_output(call_id: str, output: str) -> dict[str, Any]:
    """A ``custom_tool_call_output`` response item."""
    return {
        "type": "response_item",
        "payload": {"type": "custom_tool_call_output", "call_id": call_id, "output": output},
    }


def codex_reasoning() -> dict[str, Any]:
    """A ``reasoning`` response item (dropped by the translator)."""
    return {
        "type": "response_item",
        "payload": {"type": "reasoning", "summary": [], "content": None, "encrypted_content": "xx"},
    }


def codex_event_msg(msg_type: str = "agent_message", message: str = "hi") -> dict[str, Any]:
    """An ``event_msg`` line (TUI mirror; dropped by the translator)."""
    return {"type": "event_msg", "payload": {"type": msg_type, "message": message}}


class CodexRolloutWriter:
    """Writes Codex CLI rollout JSONL transcripts under a fake sessions directory."""

    def __init__(self, sessions_dir: Path) -> None:
        self.sessions_dir = sessions_dir
        sessions_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _dump(entries: list[dict[str, Any]]) -> str:
        return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries)

    def write(self, session_id: str, entries: list[dict[str, Any]]) -> str:
        path = self.sessions_dir / f"rollout-{session_id}.jsonl"
        path.write_text(self._dump(entries), encoding="utf-8")
        return str(path)

    def write_raw(self, session_id: str, text: str) -> str:
        """Write arbitrary bytes (for malformed-line / tolerance tests)."""
        path = self.sessions_dir / f"rollout-{session_id}.jsonl"
        path.write_text(text, encoding="utf-8")
        return str(path)
