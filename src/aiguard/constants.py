# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.


class AIGuardConstants(object):
    # tags
    SESSION_ID_TAG = "ai_guard.usr.session_id"
    USER_ID_TAG = "ai_guard.usr.id"
    SUBAGENT_ID_TAG = "ai_guard.subagent.id"
    SUBAGENT_TYPE_TAG = "ai_guard.subagent.type"
    CODING_AGENT_TAG = "ai_guard.coding_agent"
    MODEL_TAG = "ai_guard.model"

    # resources
    HOOK_RESOURCE = "ai_guard_hook"

    # operation names
    SESSION_START = "session-start"
    SESSION_END = "session-end"
    SUBAGENT_START = "subagent-start"
    SUBAGENT_STOP = "subagent-stop"
    PRE_TOOL = "pre-tool"
    POST_TOOL = "post-tool"
    USER_PROMPT_EXPANSION = "user-prompt-expansion"

    # CODING AGENTS
    CLAUDE_CODE = "claude_code"
    CLAUDE_MIN_VERSION = "2.1.139"

    # Privacy mode — forwarded to the AI Guard client as its ``mode`` argument,
    # controlling what message content is surfaced in the UI. CODING_AGENT (our
    # default) redacts every message's content to ``[redacted]`` regardless of
    # the evaluation decision; DEFAULT keeps full contents for every evaluation.
    PRIVACY_MODE_ENV = "DD_AI_GUARD_PRIVACY_MODE"
    PRIVACY_MODE_CODING_AGENT = "CODING_AGENT"
    PRIVACY_MODE_DEFAULT = "DEFAULT"

    # Installer services
    LAUNCHD_LABEL = "com.datadoghq.ai-guard"
    SYSTEMD_UNIT_NAME = "ai-guard.service"
    SYSTEMD_SOCKET_NAME = "ai-guard.socket"
    LAUNCHD_SOCKET_NAME = "Listener"

    # Proxy defaults (used by the proxy server, the hook client, and the installer)
    PROXY_HOST_DEFAULT = "127.0.0.1"
    PROXY_PORT_DEFAULT = 29279
    PROXY_URL_DEFAULT = f"http://{PROXY_HOST_DEFAULT}:{PROXY_PORT_DEFAULT}"
    PROXY_IDLE_TIMEOUT_DEFAULT = 0  # run forever

    # Upstream defaults
    ANTHROPIC_UPSTREAM_DEFAULT = "https://api.anthropic.com"
