# academic-mcp Docker image — operational notes

## Building

Build **on an ARM64 host** (e.g. an Apple Silicon Mac running Linux, or any ARM64 server). Do **not** cross-build on an x86 Mac:
`uv sync` downloads platform-specific wheels (torch, tokenizers, chromadb) and
the resulting image will not run on ARM64.

```bash
cd /path/to/academic-mcp
docker build -f docker/Dockerfile -t academic-mcp:latest .
```

Build takes ~3–5 minutes on ARM64. Image size ~800 MB–1.2 GB (majority is
PyTorch for the cross-encoder reranker).

## Running

Use `deploy/docker-run-mcp.sh` for the canonical invocation. Quick one-liner:

```bash
docker run -d \
  --name academic_mcp \
  --restart always \
  --network YOUR-DOCKER-NETWORK \
  -v /home/YOURUSER/zotero-mirror:/zotero:ro \
  -v /home/YOURUSER/academic-mcp-config:/config \
  -v /home/YOURUSER/academic-mcp-cache:/var/cache/academic-mcp \
  academic-mcp:latest
```

## Configuration

Drop a `.env` file into the `/config` volume mount. It is sourced by
`entrypoint.sh` before the server starts. The canonical contents are
documented in `deploy/docker-run-mcp.sh`.

Environment variables set directly on `docker run -e` take priority over the
config file.

## Verifying

```bash
# Check the healthcheck endpoint from inside the container
docker exec academic_mcp curl -s http://127.0.0.1:8765/healthz
# → ok

# Check logs
docker logs academic_mcp --tail 30

# Check Docker's own health state
docker inspect academic_mcp --format '{{.State.Health.Status}}'
# → healthy  (after ~30s)
```

## Updating

```bash
# Rebuild and replace in one step
cd /path/to/academic-mcp
git pull
docker build -f docker/Dockerfile -t academic-mcp:latest .
docker stop academic_mcp && docker rm academic_mcp
deploy/docker-run-mcp.sh
```

The Chroma index and PDF cache are on a named volume (`/var/cache/academic-mcp`),
so they survive image updates.

## Volumes

| Container path | Purpose | Writable? |
|---|---|---|
| `/zotero` | Zotero mirror (sqlite + storage/) | No (`:ro`) |
| `/config` | `.env` secrets file | No (`:ro`) |
| `/var/cache/academic-mcp` | Chroma index, PDF cache, article cache | Yes |

## Networking

The container joins an existing Docker network (`YOUR-DOCKER-NETWORK` in
the default `docker-run-mcp.sh`). Other containers on the same network (e.g.
`llama_embed`, `scrapling_mcp`) are reachable by container name.

Port 8765 is exposed but not published to the host — your reverse proxy
routes traffic at the network level.

## ARM64 Chromium note

The Scrapling sidecar handles all stealth browser work. If the sidecar is
unavailable, the MCP server continues to function via its OA fetch pipeline
(Unpaywall, CORE, S2, Zotero-local). Set `SCRAPLING_MCP_URL=` (empty) to
intentionally disable the sidecar.
