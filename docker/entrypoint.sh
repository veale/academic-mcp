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

exec uv run python -m academic_mcp --transport sse --port "${MCP_PORT:-8765}"
