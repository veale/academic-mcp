# Single-container OAuth deployment

The Docker image includes both the academic MCP server and a pinned OAuth 2.1
gateway. The gateway is the only public listener; the MCP process listens on
loopback. This avoids the common broken arrangement where an OAuth proxy strips
its access token and a second static bearer-token layer rejects the forwarded
request.

Set these values in `/config/.env`:

```dotenv
OAUTH_ENABLED=true
OAUTH_EXTERNAL_URL=https://academic.example.com
OAUTH_PASSWORD_HASH=$2a$12$replace_with_a_bcrypt_hash
MCP_PORT=8765
MCP_INTERNAL_PORT=8766
MCP_API_KEY=
```

Mount `/var/cache/academic-mcp` persistently. OAuth registrations, signing keys,
and feedback reports then survive container replacement. Route the public host
to port 8765 and configure clients with:

```text
https://academic.example.com/mcp
```

Discovery endpoints are served by the bundled gateway. The container healthcheck
probes the loopback backend directly, so it does not need an OAuth token.

If separate containers are preferred, keep `MCP_API_KEY` on the backend and pass
the same value to the gateway as `PROXY_BEARER_TOKEN`. Never put two independent
bearer checks in series without configuring that upstream credential.
