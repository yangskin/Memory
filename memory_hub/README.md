# Memory Hub

Memory Hub is the independently deployable HTTPS event service for the local
Memory MCP. The local MCP and this package communicate only through the
versioned JSON contracts in `contracts/v1/`.

## Development

```bash
uv sync --all-extras --dev
uv run pytest
uv run memory-hub-api
uv run memory-hub-worker
```

The default brief provider is fake and never contacts an LLM. Database-backed
routes and workers are added in subsequent implementation phases.

## Production By IP

The Compose deployment exposes only Caddy on ports `80` and `443`; PostgreSQL,
the API, and the worker remain on the private Compose network. Copy
`.env.example` to `.env`, set `MEMORY_HUB_PUBLIC_IP` to the externally routable
host IP, set a strong `POSTGRES_PASSWORD`, then run:

```bash
docker compose up -d --build
docker compose ps
curl --cacert /tmp/memory-hub-root.crt https://YOUR_PUBLIC_IP/healthz
```

Bare IP addresses cannot obtain a browser-trusted public TLS certificate. Caddy
therefore uses its internal CA. Export the CA certificate after startup and
install it in every local MCP client trust store:

```bash
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt /tmp/memory-hub-root.crt
```

For a publicly trusted certificate without installing a CA certificate, use a
DNS name instead of an IP address.

## Package Installation

```bash
pip install ./memory_hub
```