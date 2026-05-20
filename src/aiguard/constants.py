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

    # CODING AGENTS
    CLAUDE_CODE = "claude_code"

    # Installer services
    LAUNCHD_LABEL = "com.datadoghq.ai-guard"
    SYSTEMD_UNIT_NAME = "ai-guard.service"

    # Proxy defaults (used by the proxy server, the hook client, and the installer)
    PROXY_HOST_DEFAULT = "127.0.0.1"
    PROXY_PORT_DEFAULT = 29279
    PROXY_URL_DEFAULT = f"http://{PROXY_HOST_DEFAULT}:{PROXY_PORT_DEFAULT}"
