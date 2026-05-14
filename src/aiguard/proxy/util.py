from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator


@dataclass
class SSEEvent:
    """A dispatched Server-Sent Event."""

    event: str = "message"
    data: str = ""
    id: str | None = None
    retry: int | None = None


def parse_sse(body: bytes) -> Iterator[SSEEvent]:
    """
    Parse a complete Server-Sent Events byte body into SSEEvent objects.

    Implements the WHATWG SSE spec closely enough for any real-world producer
    (Anthropic, OpenAI, etc.):

      - Handles \\n, \\r\\n, and bare \\r line terminators.
      - Strips a single optional leading space after the field colon.
      - Treats lines starting with ':' as comments.
      - Joins multi-line `data:` fields with '\\n'.
      - Carries `id` across events (sticky) per spec; ignores ids containing NUL.
      - Skips dispatch when the data buffer is empty (per spec).
      - Strips a leading UTF-8 BOM if present.
      - Tolerates invalid UTF-8 by replacing, never crashing.
      - Does NOT dispatch a trailing unterminated event (truncation is the caller's
        problem to detect at the transport layer).

    Args:
        body: The full SSE response body as bytes.

    Yields:
        SSEEvent for each dispatched event, in order.
    """
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise TypeError(f"body must be bytes-like, got {type(body).__name__}")

    buf = bytes(body)

    # SSE spec: strip a leading UTF-8 BOM if present.
    if buf.startswith(b"\xef\xbb\xbf"):
        buf = buf[3:]

    data_lines: list[str] = []
    event_type = ""
    last_id: str | None = None
    retry: int | None = None

    def build_event() -> SSEEvent | None:
        """Build an event from the current field buffers; reset per-event state."""
        nonlocal data_lines, event_type
        if not data_lines:
            # Spec: empty data buffer => do not dispatch. Still reset event type.
            event_type = ""
            return None
        evt = SSEEvent(
            event=event_type or "message",
            data="\n".join(data_lines),
            id=last_id,
            retry=retry,
        )
        data_lines = []
        event_type = ""
        return evt

    def process_line(line: str) -> SSEEvent | None:
        nonlocal event_type, last_id, retry

        if line == "":
            return build_event()

        if line.startswith(":"):
            return None  # SSE comment / keep-alive

        if ":" in line:
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = line, ""

        if field == "event":
            event_type = value
        elif field == "data":
            data_lines.append(value)
        elif field == "id":
            if "\x00" not in value:
                last_id = value
        elif field == "retry":
            if value.isdigit():
                retry = int(value)
        # Unknown fields ignored per spec.
        return None

    # Walk the bytes, splitting on \n, \r\n, or bare \r. Decode each complete
    # line individually so a multi-byte UTF-8 codepoint can never be split mid-
    # decode (and a malformed line is replaced rather than crashing the parser).
    n = len(buf)
    start = 0
    i = 0
    while i < n:
        b = buf[i]
        if b == 0x0A:  # \n
            line = buf[start:i].decode("utf-8", errors="replace")
            evt = process_line(line)
            if evt is not None:
                yield evt
            i += 1
            start = i
        elif b == 0x0D:  # \r or \r\n
            line = buf[start:i].decode("utf-8", errors="replace")
            evt = process_line(line)
            if evt is not None:
                yield evt
            # Look ahead for the \n in \r\n. Safe here because `body` is complete:
            # there is no "we might get more bytes later" case.
            if i + 1 < n and buf[i + 1] == 0x0A:
                i += 2
            else:
                i += 1
            start = i
        else:
            i += 1
    # Trailing unterminated event is intentionally dropped (see docstring).
