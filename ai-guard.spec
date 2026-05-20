# -*- mode: python ; coding: utf-8 -*-

# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

a = Analysis(
    ["src/aiguard/__main__.py"],
    pathex=["src"],
    binaries=[],
    datas=[],
    hiddenimports=[
        "aiguard",
        "aiguard.claude",
        "aiguard.hooks",
        "aiguard.hooks.hooks",
        "aiguard.proxy",
        "aiguard.proxy.server",
        "aiguard.claude.proxy",
        # aiohttp core + C extensions
        "aiohttp",
        "aiohttp.web",
        "aiohttp.web_app",
        "aiohttp.web_exceptions",
        "aiohttp.web_middlewares",
        "aiohttp.web_protocol",
        "aiohttp.web_request",
        "aiohttp.web_response",
        "aiohttp.web_routedef",
        "aiohttp.web_runner",
        "aiohttp.web_server",
        "aiohttp.web_urldispatcher",
        "aiohttp.client",
        "aiohttp.client_reqrep",
        "aiohttp.connector",
        "aiohttp.streams",
        "aiohttp.payload",
        "aiohttp.helpers",
        "aiohttp.tcp_helpers",
        "aiohttp._helpers",
        "aiohttp._http_parser",
        "aiohttp._http_writer",
        "aiohttp._websocket",
        # aiohttp dependencies
        "multidict",
        "multidict._multidict",
        "yarl",
        "frozenlist",
        "frozenlist._frozenlist",
        "propcache",
        "propcache._helpers",
        "ddtrace",
        "ddtrace.trace",
        "ddtrace.internal",
        "ddtrace.ext",
        "ddtrace.propagation",
        "ddtrace.constants",
        "ddtrace.appsec.ai_guard",
        "ddtrace.contrib",
        "ddtrace.contrib.internal",
        "ddtrace.contrib.internal.trace_utils_base",
        "ddtrace.contrib.internal.subprocess",
        "ddtrace.contrib.internal.subprocess.patch",
        "ddtrace.llmobs",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # ddtrace: unused appsec submodules
        "ddtrace.appsec.ddwaf",
        "ddtrace.appsec.iast",
        "ddtrace.appsec.asm_manager",
        # ddtrace: unused heavyweight features
        "ddtrace.auto",
        "ddtrace.bootstrap",
        "ddtrace.commands",
        "ddtrace.debugging",
        "ddtrace.errortracking",
        "ddtrace.openfeature",
        "ddtrace.opentelemetry",
        "ddtrace.profiling",
        "ddtrace.runtime",
        "ddtrace.sourcecode",
        "ddtrace.testing",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ai-guard",
    debug=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=True,
    onefile=True,
)
