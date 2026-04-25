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
date +%s > "$LAST_RUN_FILE"

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

# Online .backup uses SQLite read locks — safe to run while Zotero holds the
# write lock. If it fails, exit non-zero; DO NOT cp the bare file (would
# capture an inconsistent state mid-transaction).
if ! sqlite3 "$SRC_SQLITE" ".backup '$SNAPSHOT'" 2>/tmp/zotero-sync-backup.err; then
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

echo "zotero-sync: ok."

#
# Invoked by launchd (see com.example.zotero-sync.plist).  Also safe to
# run interactively for a one-off sync.
#
# Configuration via env vars (set in the plist):
#   REMOTE_HOST        — Tailscale MagicDNS name (or IP) of the remote server
#   REMOTE_USER        — SSH user on the remote server
#   REMOTE_DIR         — target directory on the remote server (default: /home/<user>/zotero-mirror)
#   ZOTERO_SRC         — source directory (default: $HOME/Zotero)
#   SYNC_MODE          — A (default, full mirror) or B (skip storage/ — used with Nextcloud WebDAV)
#   DEBOUNCE_SECONDS   — skip if we ran within this many seconds (default: 60)
set -euo pipefail

ZOTERO_SRC="${ZOTERO_SRC:-$HOME/Zotero}"
REMOTE_DIR="${REMOTE_DIR:-/home/$REMOTE_USER/zotero-mirror}"

# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------
# launchd's WatchPaths fires frequently while Zotero is writing.  Skip this
# invocation if we ran recently.
DEBOUNCE_SECONDS="${DEBOUNCE_SECONDS:-60}"
LAST_RUN_FILE="$HOME/.cache/zotero-sync/.last-run"
mkdir -p "$(dirname "$LAST_RUN_FILE")"

if [ -f "$LAST_RUN_FILE" ]; then
    NOW=$(date +%s)
    LAST=$(cat "$LAST_RUN_FILE" 2>/dev/null || echo 0)
    ELAPSED=$((NOW - LAST))
    if [ "$ELAPSED" -lt "$DEBOUNCE_SECONDS" ]; then
        echo "zotero-sync: debounce ($ELAPSED s since last run, threshold $DEBOUNCE_SECONDS s); skipping."
        exit 0
    fi
fi
date +%s > "$LAST_RUN_FILE"

# ---------------------------------------------------------------------------
# SQLite snapshot
# ---------------------------------------------------------------------------
# Produce a consistent snapshot of zotero.sqlite using SQLite's backup API.
# The API uses read locks, so it succeeds even while Zotero holds the
# exclusive write lock — but it can fail if Zotero is in the middle of a
# transaction.  On failure, fall back to a plain file copy; the server-side
# shadow-copy code catches any partial state.
SNAPSHOT_DIR="$HOME/.cache/zotero-sync"
SNAPSHOT="$SNAPSHOT_DIR/zotero.sqlite.snapshot"
mkdir -p "$SNAPSHOT_DIR"

SRC_SQLITE="$ZOTERO_SRC/zotero.sqlite"

if [ ! -f "$SRC_SQLITE" ]; then
    echo "zotero-sync: no zotero.sqlite at $SRC_SQLITE — aborting." >&2
    exit 1
fi

if command -v sqlite3 >/dev/null 2>&1; then
    # .backup over a lock-aware connection — safe concurrent with Zotero.
    if ! sqlite3 "$SRC_SQLITE" ".backup '$SNAPSHOT'" 2>/dev/null; then
        echo "zotero-sync: sqlite3 .backup failed; falling back to cp." >&2
        cp "$SRC_SQLITE" "$SNAPSHOT"
    fi
else
    cp "$SRC_SQLITE" "$SNAPSHOT"
fi

# ---------------------------------------------------------------------------
# rsync setup
# ---------------------------------------------------------------------------

# macOS `rsync` is ancient (2.6.9).  Prefer the Homebrew-installed rsync
# if present; it has proper --partial and faster delta handling.
if command -v /opt/homebrew/bin/rsync >/dev/null 2>&1; then
    RSYNC=/opt/homebrew/bin/rsync
else
    RSYNC=rsync
fi

# Mode B (Nextcloud WebDAV): attachments sync themselves through Nextcloud,
# so skip the local storage/ directory entirely.
if [ "${SYNC_MODE:-A}" = "B" ]; then
    RSYNC_EXCLUDES="--exclude=storage/"
else
    RSYNC_EXCLUDES=""
fi

# ---------------------------------------------------------------------------
# rsync phase 1: everything except the live sqlite
# ---------------------------------------------------------------------------
# The live sqlite is excluded here because it's always in flux; we ship a
# consistent snapshot separately in phase 2.
"$RSYNC" \
    --archive \
    --delete \
    --partial \
    --compress --compress-level=1 \
    --exclude='zotero.sqlite' \
    --exclude='zotero.sqlite-journal' \
    --exclude='zotero.sqlite-wal' \
    --exclude='zotero.sqlite-shm' \
    --exclude='zotero.sqlite.bak' \
    --exclude='locate/' \
    --exclude='logs/' \
    --exclude='translators-backup.zip' \
    --exclude='tmp/' \
    ${RSYNC_EXCLUDES:-} \
    -e 'ssh -o BatchMode=yes -o ConnectTimeout=15' \
    "$ZOTERO_SRC/" \
    "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"

# ---------------------------------------------------------------------------
# rsync phase 2: the snapshot
# ---------------------------------------------------------------------------
# Rename into place atomically: sync as a .new file, then ssh mv it over the
# existing target.  Without this, the container could read a half-written
# sqlite file if rsync is interrupted.
"$RSYNC" \
    --archive \
    --partial \
    --compress --compress-level=1 \
    -e 'ssh -o BatchMode=yes -o ConnectTimeout=15' \
    "$SNAPSHOT" \
    "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/zotero.sqlite.new"

ssh -o BatchMode=yes -o ConnectTimeout=15 \
    "$REMOTE_USER@$REMOTE_HOST" \
    "mv -f $REMOTE_DIR/zotero.sqlite.new $REMOTE_DIR/zotero.sqlite"

# ---------------------------------------------------------------------------
# Freshness watermark
# ---------------------------------------------------------------------------
# Record a success timestamp on the remote server so the MCP container can
# expose "how old is this mirror" in diagnostics.
ssh -o BatchMode=yes -o ConnectTimeout=15 \
    "$REMOTE_USER@$REMOTE_HOST" \
    "date -u +%Y-%m-%dT%H:%M:%SZ > $REMOTE_DIR/.last-sync"
