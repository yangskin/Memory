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

The default brief provider is fake and never contacts an LLM.

## Production By IP

Docker publishes Caddy on all host interfaces, so no application-level port
forwarding or public-IP bind is required. Set `MEMORY_HUB_PUBLIC_IP` to the
externally routable address assigned to the host.

This still requires the cloud security group/firewall to permit inbound TCP
`80` and `443` to this instance. PostgreSQL, the API, and the worker do not
publish ports and remain on the private Compose network.

### Configure

`.env` is ignored by Git. Create it with restrictive permissions:

```bash
cd memory_hub
umask 077
cp .env.example .env
```

Set these values in `.env`:

```dotenv
MEMORY_HUB_PUBLIC_IP=<externally-routable-ip>
POSTGRES_PASSWORD=<a long random value>
```

Generate a password locally without printing it:

```bash
openssl rand -hex 32
```

### Start And Verify

```bash
cd memory_hub
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 api worker caddy
```

The API container runs `alembic upgrade head` before it starts. A healthy
stack has `postgres`, `api`, `worker`, and `caddy` running; only Caddy listens
on host ports.

### Trust The IP Certificate

Public certificate authorities do not issue browser-trusted certificates for
bare IP addresses. Caddy therefore uses an internal CA. Export its root
certificate after startup and install it in each MCP client's OS trust store:

```bash
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt /tmp/memory-hub-root.crt
```

Run these checks from the host after exporting the certificate:

```bash
# Reliable local TLS/routing test; preserves the public IP as the TLS host.
curl --fail --cacert /tmp/memory-hub-root.crt \
	--connect-to <externally-routable-ip>:443:127.0.0.1:443 \
	https://<externally-routable-ip>/healthz

# Tests the actual external route. It can fail on hosts without NAT hairpin;
# in that case run the same command from a separate Internet-connected host.
curl --fail --cacert /tmp/memory-hub-root.crt \
	https://<externally-routable-ip>/healthz
```

Expected response is a JSON health object. Test endpoint accessibility from a
different network before configuring local MCP clients.

### Operate

```bash
# Follow service logs
docker compose logs -f api worker caddy

# Apply image/code updates
docker compose up -d --build

# Stop services without deleting the PostgreSQL volume
docker compose down

# Remove all service data. This cannot be undone.
docker compose down -v
```

Back up the database volume before upgrades or destructive operations:

```bash
docker compose exec -T postgres pg_dump -U memory_hub memory_hub > memory-hub-backup.sql
```

For publicly trusted TLS without installing a CA certificate, use a DNS name
instead of an IP address.

## Package Installation

```bash
pip install ./memory_hub
```