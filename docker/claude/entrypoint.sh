#!/bin/bash
# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

set -euo pipefail

# Wait up to 60s for $1 to exist as a regular file. On a fresh `docker compose
# up`, the mitmproxy-data volume is empty and mitmproxy needs a moment to
# generate the CA cert; short-form `depends_on` does not wait for that.
wait_for_file() {
    local path="$1" i
    for i in $(seq 600); do
        if [[ -f "$path" ]]; then
            return 0
        fi
        sleep 0.1
    done
    return 1
}

# Trust the mitmproxy CA so HTTPS_PROXY traffic verifies (used only for the
# debugging proxy; ai-guard's hooks run in-process and need no service).
if [[ -n "${CERT:-}" ]]; then
    if wait_for_file "$CERT"; then
        cat /etc/ssl/certs/ca-certificates.crt "$CERT" > /tmp/ca-bundle.pem
    else
        echo "entrypoint: timed out waiting for $CERT; HTTPS_PROXY traffic will fail TLS verification" >&2
        exit 1
    fi
else
    cp /etc/ssl/certs/ca-certificates.crt /tmp/ca-bundle.pem
fi

# AI Guard is wired into Claude Code via hooks (settings.json); they invoke
# `ai-guard hook ...` in-process, so there is nothing to start here.
exec "$@"