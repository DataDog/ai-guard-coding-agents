# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

FROM debian:12-slim AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    DD_FAST_BUILD=1

# ── System deps ───────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl=7.88.1-10+deb12u14 \
    ca-certificates=20230311+deb12u1 \
    git=1:2.39.5-0+deb12u3 \
    python3=3.11.2-1+b1 \
    python3-dev=3.11.2-1+b1 \
    python3-pip=23.0.1+dfsg-1 \
    build-essential=12.9 \
    autoconf=2.71-3 \
    automake=1:1.16.5-1.3 \
    libtool=2.4.7-7~deb12u1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /usr/local/bin/uv

# ── Rust ───────────────────────────────────────────────────────────────
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

RUN git config --global http.version HTTP/1.1

WORKDIR /build

# ── Dependencies (cached unless pyproject.toml/uv.lock change) ────────────────
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --extra build --no-install-project

# ── Binary compilation (rebuilt when source changes) ──────────────────────────
COPY ai-guard.spec ./
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --extra build && uv run pyinstaller ai-guard.spec


FROM debian:12-slim

ENV DEBIAN_FRONTEND=noninteractive

# ── System and demo deps ───────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl=7.88.1-10+deb12u14 \
    ca-certificates=20230311+deb12u1 \
    dumb-init=1.2.5-2 \
    git=1:2.39.5-0+deb12u3 \
    openssh-client=1:9.2p1-2+deb12u9 \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js 22 (required by Claude Code) ─────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs=22.22.2-1nodesource1 \
    && rm -rf /var/lib/apt/lists/*

# ── Non-root user ─────────────────────────────────────────────────────────────
RUN groupadd -g 1000 claude && useradd -l -m -s /bin/bash -u 1000 -g 1000 claude

# ── Entrypoint ────────────────────────────────────────────────────────────────
COPY docker/claude/entrypoint.sh /entrypoint.sh

# ── Claude settings ───────────────────────────────────────────────────────────
COPY docker/claude/claude-settings.json /home/claude/.claude/settings.json

# ── Install ai-guard ─────────────────────────────────────────────────────────
COPY --from=builder /build/dist/ai-guard /home/claude/.local/share/ai-guard

# ── Fix permissions ───────────────────────────────────────────────────────────
RUN mkdir -p /home/claude/.local/bin \
    && ln -s /home/claude/.local/share/ai-guard/ai-guard /home/claude/.local/bin/ai-guard \
    && chown -R claude:claude /home/claude \
    && chmod 755 /entrypoint.sh

USER claude
ENV HOME=/home/claude \
    NPM_CONFIG_PREFIX=/home/claude/.npm-global \
    CLAUDE_CODE_NO_FLICKER=1 \
    SSL_CERT_FILE=/tmp/ca-bundle.pem \
    REQUESTS_CA_BUNDLE=/tmp/ca-bundle.pem \
    GIT_SSL_CAINFO=/tmp/ca-bundle.pem \
    CERT=/mitmproxy-certs/mitmproxy-ca-cert.pem

ENV PATH="/home/claude/.local/bin:/home/claude/.npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# ── Claude Code CLI ───────────────────────────────────────────────────────────
RUN npm install -g @anthropic-ai/claude-code@2.1.139

# ── Working directory for interactive sessions ────────────────────────────────
WORKDIR /home/claude

ENTRYPOINT ["dumb-init", "--", "/entrypoint.sh"]
CMD ["sleep", "infinity"]