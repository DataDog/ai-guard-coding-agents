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
