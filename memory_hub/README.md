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

## Production With A Domain Certificate

Docker publishes Caddy on all host interfaces, so no application-level port
forwarding or public-IP bind is required. Configure the DNS `A` and/or `AAAA`
record for your public host to the server's public address.

This still requires the cloud security group/firewall to permit inbound TCP
`80` and `443` to this instance. PostgreSQL, the API, and the worker do not
publish ports and remain on the private Compose network.

### Configure

`.env` and `user_config.local.json` are ignored by Git. With the issued
`<public-hostname>.crt` and `<public-hostname>.key` already in `certs/`,
initialize the host with:

```bash
cd memory_hub
./bootstrap.sh memory.example.com <project-id> deployment
```

The script uses the uploaded `certs/root_bundle.crt` intermediate certificate
(or downloads the public TrustAsia intermediate if it is absent), creates
`certs/fullchain.pem`, copies the private key as `certs/privkey.pem`, and
creates `.env` with a locally generated PostgreSQL password. It starts the
Compose project, waits for it to become healthy, then writes a one-time,
least-privilege Token to `../user_config.local.json` with mode `0600`. It never
prints the password or Token. Supply a stable project ID explicitly; the user
argument defaults to `deployment`.

The local configuration is intentionally not overwritten on a subsequent
Bootstrap run. To rotate a credential, create a new Token, update the ignored
file, and revoke the former Token ID.

### Install The Issued Certificate

The bootstrap script produces these Caddy inputs:

```text
memory_hub/certs/fullchain.pem
memory_hub/certs/privkey.pem
```

The files are mounted read-only into Caddy and are ignored by Git. See
[`certs/README.md`](certs/README.md) for required PEM contents and permissions.

### Verify

```bash
cd memory_hub
docker compose -p <project-id> ps
docker compose -p <project-id> logs --tail=100 api worker caddy
```

The API container runs `alembic upgrade head` before it starts. A healthy
stack has `postgres`, `api`, `worker`, and `caddy` running; only Caddy listens
on host ports.

Verify the trusted certificate and API after startup:

```bash
curl --fail https://memory.example.com/healthz
```

The expected response is a JSON health object. Run this from a different
network if the host does not support NAT hairpin routing.

### Operate

```bash
# Follow service logs
docker compose -p <project-id> logs -f api worker caddy

# Apply image/code updates
docker compose -p <project-id> up -d --build

# Stop services without deleting the PostgreSQL volume
docker compose -p <project-id> down

# Remove all service data. This cannot be undone.
docker compose -p <project-id> down -v
```

### Provision A Local MCP Token

Issue a least-privilege token for one local MCP user and project. The command
prints the secret once; set it only in that MCP process environment, never in a
repository file or shell history.

```bash
docker compose -p <project-id> exec api memory-hub token create \
	--project <project-id> \
	--user <user-id> \
	--scope events:write \
	--scope context:read
```

Revoke a token by ID when a device or credential is no longer trusted:

```bash
docker compose -p <project-id> exec api memory-hub token revoke --token-id <token-id>
```

Query token IDs, users, scopes, and revocation status for a project:

```bash
docker compose -p <project-id> exec api memory-hub token list --project <project-id>
```

The raw token is intentionally never queryable because only its hash is stored.
If the one-time value is lost, create a replacement token and revoke the old ID.

Back up the database volume before upgrades or destructive operations:

```bash
docker compose -p <project-id> exec -T postgres pg_dump -U memory_hub memory_hub > memory-hub-backup.sql
```

## Package Installation

```bash
pip install ./memory_hub
```