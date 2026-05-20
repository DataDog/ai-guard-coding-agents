"""Poll a TCP endpoint until it accepts connections.

Equivalent to ``nc -z $host $port`` in a 0.1s × N loop — same semantics as
``docker/claude/entrypoint.sh:4-14`` — but pure stdlib so it works on every
platform without depending on ``nc``.
"""

from __future__ import annotations

import socket
import time


def wait_ready(host: str, port: int, timeout: float = 5.0, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(min(0.5, interval))
        try:
            if sock.connect_ex((host, port)) == 0:
                return True
        finally:
            sock.close()
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
