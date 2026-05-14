#!/bin/bash
set -euo pipefail

# Wait up to 5s for $1:$2 to accept TCP connections.
wait_for_port() {
    local host="$1" port="$2" i
    for i in $(seq 50); do
        if nc -z "$host" "$port" 2>/dev/null; then
            return 0
        fi
        sleep 0.1
    done
    return 1
}

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

# Demo repo: cloned on first start. /home/claude is a named volume that hides
# any image-time clone, so we do it here instead. HTTPS_PROXY points at the
# mitmproxy sidecar, which may not be reachable yet — wait for it first.
_repo_dir="/home/claude/claude-code-attack-vectors-poc"
if [ ! -d "$_repo_dir/.git" ]; then
    if [ -n "$HTTPS_PROXY" ]; then
        _hp="${HTTPS_PROXY#*://}"
        _hp="${_hp%%/*}"
        wait_for_port "${_hp%:*}" "${_hp##*:}"
        unset _hp
    fi
    rm -rf "$_repo_dir"
    git clone https://github.com/manuel-alvarez-alvarez/claude-code-attack-vectors-poc.git "$_repo_dir"
fi
unset _repo_dir

# Run the proxy and the user command as siblings. Running the agent against a
# dead proxy would silently bypass AI Guard, so an unexpected proxy exit is
# fatal; on the normal path the app's exit code is propagated.

proxy_pid=""
app_pid=""

cleanup() {
    trap - EXIT INT TERM
    [[ -n "$app_pid"   ]] && kill -TERM "$app_pid"   2>/dev/null || true
    [[ -n "$proxy_pid" ]] && kill -TERM "$proxy_pid" 2>/dev/null || true
    [[ -n "$app_pid"   ]] && wait "$app_pid"   2>/dev/null || true
    [[ -n "$proxy_pid" ]] && wait "$proxy_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

ai-guard proxy &
proxy_pid=$!

if ! wait_for_port 127.0.0.1 "${DD_AI_GUARD_PROXY_PORT:-29279}"; then
    echo "entrypoint: ai-guard proxy did not become ready in time" >&2
    exit 1
fi

"$@" &
app_pid=$!

first_status=0
exited_pid=""
wait -n -p exited_pid "$proxy_pid" "$app_pid" || first_status=$?

if [[ "$exited_pid" == "$app_pid" ]]; then
    exit "$first_status"
fi

echo "entrypoint: ai-guard proxy exited unexpectedly (status $first_status)" >&2
exit 1