# Reverse-proxy configuration for academic-mcp

Configure two routes in your reverse proxy (Caddy, Traefik, Nginx, Cosmos, etc.)
to expose the `academic_mcp` container.

Both routes point at the **same container**.  Which auth path is active
depends solely on whether `MCP_API_KEY` is set in the container env.

---

## Route 1 — Tailscale-only (no public DNS)

| Field | Value |
|---|---|
| Name | `academic-mcp-tailscale` |
| Upstream | `http://academic_mcp:8765` |
| Hostname | `mcp.YOUR-TAILNET.ts.net` |
| Authentication | None (Tailscale provides it) |
| TLS | Internal cert (Tailscale / self-signed) |
| Public DNS | No — MagicDNS private record only |

This is the default mode.  Leave `MCP_API_KEY` empty in `/config/.env`.
Access from MacBook and phone requires Tailscale to be connected.

---

## Route 2 — Public hostname with API-key auth

| Field | Value |
|---|---|
| Name | `academic-mcp-public` |
| Upstream | `http://academic_mcp:8765` |
| Hostname | `mcp.YOUR-PUBLIC-DOMAIN.tld` |
| Authentication | None at proxy level (middleware handles it) |
| TLS | Let's Encrypt (via your reverse proxy) |
| Public DNS | A record pointing at your server's public IP |

**Requires** `MCP_API_KEY` to be set and non-empty in the container env.
The `_ApiKeyMiddleware` in `auth.py` rejects every request that does not
carry `Authorization: Bearer <key>`.

Optional extra hardening: restrict source IPs at the proxy level if your
clients have stable IP ranges (home ISP, Tailscale exit node, etc.).

---

## Switching between modes

Both routes can be active simultaneously — they point at the same container.
The auth level (none vs. API key) is toggled by setting/unsetting `MCP_API_KEY`
in `/config/.env` and restarting the container:

```bash
# Enable API-key auth (public access)
echo "MCP_API_KEY=$(openssl rand -hex 32)" >> /home/YOURUSER/academic-mcp-config/.env
docker restart academic_mcp

# Disable API-key auth (Tailscale-only)
# Remove or blank out MCP_API_KEY in /home/YOURUSER/academic-mcp-config/.env
docker restart academic_mcp
```

**Rule**: turning on public access **requires** a non-empty `MCP_API_KEY`.
The reverse proxy does not enforce this — the container does.  Configure the
proxy route first, then set the key, then restart.

---

## Example: Cosmos YAML snippet

If using [Cosmos Cloud](https://github.com/azukaar/Cosmos-Server) as your
reverse proxy, the YAML config looks like:

```yaml
# Tailscale route
- name: academic-mcp-tailscale
  target: http://academic_mcp:8765
  host: mcp.YOUR-TAILNET.ts.net
  useHTTPS: false      # Tailscale handles TLS termination

# Public route
- name: academic-mcp-public
  target: http://academic_mcp:8765
  host: mcp.YOUR-PUBLIC-DOMAIN.tld
  useHTTPS: true
  certProvider: letsencrypt
```

Adjust field names to match your Cosmos version.
