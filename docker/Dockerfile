# ── Frontend builder ─────────────────────────────────────────────────────────
FROM node:20-alpine AS frontend-builder
WORKDIR /frontend
COPY src/academic_mcp/webapp/package.json ./
RUN npm install
COPY src/academic_mcp/webapp/ ./
RUN npm run build
# outDir is '../webapp_dist' (vite.config.ts), so output lands at /webapp_dist

# ── Runtime image ─────────────────────────────────────────────────────────────
# ARM64-native image for the academic-mcp server.
# No browser binaries: Scrapling runs as a separate sidecar container
# and is reached over HTTP via SCRAPLING_MCP_URL.
FROM python:3.12-slim-bookworm

# System deps:
#   - curl, ca-certificates: runtime HTTPS to llama-server, scrapling, etc.
#   - libmagic1: some pdf/text tooling uses libmagic via python-magic
#   - build-essential only during install; stripped after
#   - tini: proper PID 1 for clean shutdown on docker stop
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates libmagic1 sqlite3 tini \
    && rm -rf /var/lib/apt/lists/*

# uv (repo-standard package manager)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /app

# Bring in lockfile + project metadata first to maximise layer cache.
COPY pyproject.toml uv.lock README.md ./

# Install deps into /app/.venv.
#
# Extras:
#   semantic        — chromadb + einops (needed for the index)
#   semantic-openai — openai client (used to talk to a local llama-server
#                     for embeddings via SEMANTIC_PROVIDER=openai)
#   fastembed       — lightweight ONNX bi-encoder for the search_papers
#                     reranker (~150 MB total). Activated by setting
#                     RERANKER_PRIMARY=fastembed (or as fallback). Skipped
#                     by the cross-encoder reranker (semantic_search_zotero).
#
# NOT installed (kept out to keep the image small):
#   local-models    — sentence-transformers + torch (~4 GB). Without it,
#                     the local cross-encoder, the search_papers bi-encoder
#                     reranker, and SEMANTIC_PROVIDER=local all gracefully
#                     degrade. Use a hosted reranker (RERANKER_PRIMARY=openrouter)
#                     and a hosted embedder instead.
#   stealth         — scrapling browser deps; the scrapling-mcp sidecar
#                     handles browser work.
RUN uv sync --frozen --no-install-project --extra semantic --extra semantic-openai --extra fastembed

# Copy source and install the project itself.
COPY src/ ./src/
COPY --from=frontend-builder /webapp_dist/ ./src/academic_mcp/webapp_dist/
RUN uv sync --frozen --extra semantic --extra semantic-openai --extra fastembed

# Runtime env defaults.  Every one of these is overridable at `docker run` time.
ENV PYTHONUNBUFFERED=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8765 \
    PDF_CACHE_DIR=/var/cache/academic-mcp/pdfs \
    SEMANTIC_CACHE_DIR=/var/cache/academic-mcp/chroma \
    ZOTERO_SQLITE_PATH=/zotero/zotero.sqlite \
    ZOTERO_LOCAL_STORAGE=/zotero/storage \
    ZOTERO_LOCAL_ENABLED=false \
    HF_HOME=/var/cache/academic-mcp/hf \
    SENTENCE_TRANSFORMERS_HOME=/var/cache/academic-mcp/hf/sentence-transformers \
    RERANKER_PRIMARY=openrouter \
    RERANKER_FALLBACK=none

# Cache and data directories.  All writable volumes.
RUN mkdir -p /var/cache/academic-mcp/pdfs \
             /var/cache/academic-mcp/chroma \
             /var/cache/academic-mcp/articles \
             /var/cache/academic-mcp/hf \
             /config \
             /zotero

EXPOSE 8765

COPY docker/entrypoint.sh /entrypoint.sh
COPY docker/healthcheck.py /healthcheck.py
RUN chmod +x /entrypoint.sh

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python /healthcheck.py || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
