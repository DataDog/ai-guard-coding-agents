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
    datas=[
        # Service-registration templates loaded via importlib.resources.
        ("src/aiguard/installer/templates/*.in", "aiguard/installer/templates"),
    ],
    hiddenimports=[
        "aiguard",
        "aiguard.claude",
        "aiguard.claude.installer",
        "aiguard.claude.proxy",
        "aiguard.hooks",
        "aiguard.hooks.hooks",
        "aiguard.proxy",
        "aiguard.proxy.server",
        "aiguard.installer",
        "aiguard.installer.agent",
        "aiguard.installer.backup",
        "aiguard.installer.config",
        "aiguard.installer.installer",
        "aiguard.installer.paths",
        "aiguard.installer.prompt",
        # service.manager picks one of these at runtime via platform check, so
        # PyInstaller's static analyser can't see the conditional import.
        "aiguard.installer.service",
        "aiguard.installer.service.manager",
        "aiguard.installer.service.launchd",
        "aiguard.installer.service.systemd_user",
        "aiguard.installer.service.readiness",
        "aiguard.installer.service.wrapper",
        # Templates are accessed via importlib.resources.files(__package__);
        # the package itself must be importable for the lookup to find the
        # bundled .in files declared in `datas`.
        "aiguard.installer.templates",
        "rich",
        "rich.console",
        "rich.panel",
        "rich.table",
        # Lazily imported from aiguard.installer.prompt only on a real TTY.
        "pwinput",
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
    [],
    exclude_binaries=True,
    name="ai-guard",
    debug=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ai-guard",
)
