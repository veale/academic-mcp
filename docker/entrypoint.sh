#!/usr/bin/env bash
set -euo pipefail

# Load /config/.env if present.  This is the user's drop-in point for
# secrets: OPENAI_API_KEY, MCP_API_KEY, ZOTERO_API_KEY, etc.  Env vars
# set on the docker run command take priority over the file.
if [ -f /config/.env ]; then
    set -a
    # shellcheck disable=SC1091
    . /config/.env
    set +a
fi

# Sanity warn (not fail) if Zotero mirror is unreachable — the server
# starts regardless so semantic_index_status etc still work.
if [ -n "${ZOTERO_SQLITE_PATH:-}" ] && [ ! -f "${ZOTERO_SQLITE_PATH}" ]; then
    echo "warn: ZOTERO_SQLITE_PATH=${ZOTERO_SQLITE_PATH} does not exist yet"
    echo "      The MCP server will start but Zotero-backed tools will"
    echo "      return errors until the sqlite file appears."
fi

if [ "${OAUTH_ENABLED:-false}" != "true" ] && [ "${OAUTH_ENABLED:-false}" != "1" ]; then
    exec uv run python -m academic_mcp --transport "${MCP_TRANSPORT:-streamable-http}" --port "${MCP_PORT:-8765}"
fi

# Bundled OAuth mode: the gateway is the only public listener.  The MCP
# backend binds loopback and deliberately does not apply its static API-key
# middleware; the gateway has already authenticated every forwarded request.
: "${OAUTH_EXTERNAL_URL:?OAUTH_EXTERNAL_URL is required when OAUTH_ENABLED=true}"
if [ -z "${OAUTH_PASSWORD:-}" ] && [ -z "${OAUTH_PASSWORD_HASH:-}" ] \
   && [ -z "${GOOGLE_CLIENT_ID:-}" ] && [ -z "${GITHUB_CLIENT_ID:-}" ] \
   && [ -z "${OIDC_CONFIGURATION_URL:-}" ]; then
    echo "error: bundled OAuth needs OAUTH_PASSWORD/OAUTH_PASSWORD_HASH or an OAuth/OIDC provider" >&2
    exit 2
fi

public_port="${MCP_PORT:-8765}"
internal_port="${MCP_INTERNAL_PORT:-8766}"
oauth_data="${OAUTH_DATA_PATH:-/var/cache/academic-mcp/oauth}"
mkdir -p "$oauth_data"

(
    unset MCP_API_KEY
    export MCP_HOST=127.0.0.1 MCP_PORT="$internal_port"
    exec uv run python -m academic_mcp --transport streamable-http --port "$internal_port"
) &
backend_pid=$!

cleanup() {
    kill "$backend_pid" 2>/dev/null || true
    wait "$backend_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

proxy_args=(
    --external-url="$OAUTH_EXTERNAL_URL"
    --listen=":$public_port"
    --data-path="$oauth_data"
    --no-auto-tls
)
if [ -n "${OAUTH_PASSWORD:-}" ]; then
    proxy_args+=(--password="$OAUTH_PASSWORD")
elif [ -n "${OAUTH_PASSWORD_HASH:-}" ]; then
    proxy_args+=(--password-hash="$OAUTH_PASSWORD_HASH")
fi

# The proxy also reads its documented Google/GitHub/OIDC configuration from
# environment variables, so those need no translation here.
mcp-auth-proxy "${proxy_args[@]}" -- "http://127.0.0.1:$internal_port" &
proxy_pid=$!

# Exit the container if either service dies; Docker's restart policy can then
# restart the complete, consistent pair.
wait -n "$backend_pid" "$proxy_pid"
status=$?
kill "$backend_pid" "$proxy_pid" 2>/dev/null || true
wait "$backend_pid" "$proxy_pid" 2>/dev/null || true
exit "$status"
