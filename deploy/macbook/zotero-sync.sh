#!/usr/bin/env bash
# Mac → Asahi sync for academic-mcp.
# Three jobs:
#   1. SQLite snapshot via .backup (safe while Zotero is open).
#   2. rsync the snapshot to REMOTE_DIR/zotero.sqlite (atomic rename).
#   3. rsync just the .zotero-ft-cache files from ~/Zotero/storage/ to REMOTE_FT.
#
# Env vars (set in the launchd plist):
#   REMOTE_HOST, REMOTE_USER, REMOTE_DIR, REMOTE_FT, ZOTERO_SRC, DEBOUNCE_SECONDS
#
# On any failure: exit non-zero, ship nothing, leave previous good state alone.
# sqlite3 .backup failure never falls back to cp — a partial copy is worse than
# an old clean copy.
set -euo pipefail

ZOTERO_SRC="${ZOTERO_SRC:-$HOME/Zotero}"
REMOTE_DIR="${REMOTE_DIR:?REMOTE_DIR must be set}"
REMOTE_FT="${REMOTE_FT:?REMOTE_FT must be set}"
REMOTE_HOST="${REMOTE_HOST:?REMOTE_HOST must be set}"
REMOTE_USER="${REMOTE_USER:?REMOTE_USER must be set}"

# ── Debounce ────────────────────────────────────────────────────────────────
# launchd WatchPaths fires repeatedly while Zotero is writing. Skip if we ran
# within DEBOUNCE_SECONDS.
DEBOUNCE_SECONDS="${DEBOUNCE_SECONDS:-60}"
LAST_RUN_FILE="$HOME/.cache/zotero-sync/.last-run"
mkdir -p "$(dirname "$LAST_RUN_FILE")"

if [ -f "$LAST_RUN_FILE" ]; then
    NOW=$(date +%s)
    LAST=$(cat "$LAST_RUN_FILE" 2>/dev/null || echo 0)
    ELAPSED=$((NOW - LAST))
    if [ "$ELAPSED" -lt "$DEBOUNCE_SECONDS" ]; then
        echo "zotero-sync: debounce ($ELAPSED s); skip." >&2
        exit 0
    fi
fi

# ── Tools ────────────────────────────────────────────────────────────────────
# macOS ships rsync 2.6.9 — too old. Require the Homebrew version.
RSYNC=/opt/homebrew/bin/rsync
[ -x "$RSYNC" ] || { echo "zotero-sync: Homebrew rsync not found at $RSYNC — run: brew install rsync" >&2; exit 2; }
command -v sqlite3 >/dev/null || { echo "zotero-sync: sqlite3 not found — run: brew install sqlite" >&2; exit 2; }

SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=15"

# ── 1. SQLite snapshot ───────────────────────────────────────────────────────
SRC_SQLITE="$ZOTERO_SRC/zotero.sqlite"
[ -f "$SRC_SQLITE" ] || { echo "zotero-sync: $SRC_SQLITE not found" >&2; exit 2; }

SNAPSHOT_DIR="$HOME/.cache/zotero-sync"
SNAPSHOT="$SNAPSHOT_DIR/zotero.sqlite.snapshot"
mkdir -p "$SNAPSHOT_DIR"

# SQLite backup respects locking. It can read during ordinary writes, but
# cannot bypass an exclusive lock. Fail safely and keep the previous mirror.
if ! sqlite3 -readonly -cmd ".timeout 5000" "$SRC_SQLITE" ".backup '$SNAPSHOT'" 2>/tmp/zotero-sync-backup.err; then
    echo "zotero-sync: sqlite3 .backup failed — Zotero may be mid-transaction. Aborting." >&2
    cat /tmp/zotero-sync-backup.err >&2 || true
    exit 3
fi

# ── 2. Ship snapshot → REMOTE_DIR/zotero.sqlite (atomic rename) ─────────────
"$RSYNC" \
    --archive --partial --compress --compress-level=1 \
    -e "ssh $SSH_OPTS" \
    "$SNAPSHOT" \
    "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/zotero.sqlite.new"

ssh $SSH_OPTS "$REMOTE_USER@$REMOTE_HOST" \
    "mv -f '$REMOTE_DIR/zotero.sqlite.new' '$REMOTE_DIR/zotero.sqlite' && \
     date -u +%Y-%m-%dT%H:%M:%SZ > '$REMOTE_DIR/.last-sync'"

# ── 3. Ship .zotero-ft-cache files → REMOTE_FT ──────────────────────────────
# Only the plain-text extract files are synced — not PDFs or other attachments.
# This gives the MCP container full-text content for semantic indexing without
# shipping the much-larger binary files.
#
# Filter logic:
#   include all directories   — needed so rsync traverses <key>/ subdirs
#   include .zotero-ft-cache  — the target files
#   exclude everything else   — no PDFs, images, etc.
# --prune-empty-dirs: don't create <key>/ dirs for attachments without ft-cache.
# --delete-excluded: remove any previously synced files that no longer match.
"$RSYNC" \
    --archive --partial --compress --compress-level=1 \
    --delete --delete-excluded --prune-empty-dirs \
    --include='*/' \
    --include='.zotero-ft-cache' \
    --exclude='*' \
    -e "ssh $SSH_OPTS" \
    "$ZOTERO_SRC/storage/" \
    "$REMOTE_USER@$REMOTE_HOST:$REMOTE_FT/"

ssh $SSH_OPTS "$REMOTE_USER@$REMOTE_HOST" \
    "date -u +%Y-%m-%dT%H:%M:%SZ > '$REMOTE_FT/.last-sync'"

date +%s > "$LAST_RUN_FILE"
echo "zotero-sync: ok."
