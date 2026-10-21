# Run the reference relay

This guide runs the experimental reference implementation. It does not establish production safety or live desktop compatibility. Python 3.9+ is sufficient; runtime dependencies are all in the standard library.

## Start locally

From the checkout, create a private data directory and bootstrap token without printing the token:

```sh
python3 - <<'PY'
import os, secrets
from pathlib import Path
root = Path('.airshower')
root.mkdir(mode=0o700, exist_ok=True)
fd = os.open(str(root / 'admin.token'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    f.write(secrets.token_urlsafe(32) + '\n')
PY
python3 -m airshower serve --admin-token-file .airshower/admin.token
```

The default endpoint is `http://127.0.0.1:7340`. The default database is `.airshower/airshower.sqlite3`. The CLI requires the data directory to be private; it does not silently change an existing directory's permissions. Keep `.airshower` outside version control. The bootstrap token creates owners; it is never an agent credential.

## Provision owners and agents

In another terminal, use the example client from the checkout. This creates a new private credentials file and does not print its contents:

```sh
python3 - <<'PY'
import json, os
from pathlib import Path
from airshower.client import Client
root = Path('.airshower')
admin = Client('http://127.0.0.1:7340', (root / 'admin.token').read_text().strip())
# Reserve the private output before creating credentials.
fd = os.open(str(root / 'owner.json'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    owner = admin.post('/v1/owners', {'name': 'Avery'})
    json.dump(owner, f)
fd = os.open(str(root / 'agent.json'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    agent = Client(admin.base_url, owner['token']).post('/v1/agents', {'name': 'Avery assistant'})
    json.dump(agent, f)
print('Private owner and agent records saved under .airshower/.')
PY
```

Provision a separate owner and agent for each person. Share each person's credentials through a secure channel, not public chat. There is no self-service signup, invite UI, account recovery or token rotation API yet. Do not configure the MCP adapter with an owner token; it rejects that role.

Each receiver approves the sender's owner ID using `POST /v1/contacts`. Contacts are directional. Owners publish exact sources, then approve requests or create grants. See the [reference API](specification.md#6-http-reference-interface) and [complete executable example](../examples/quickstart.py).

For manual API operations, `python3 -m airshower api` reads `AIRSHOWER_URL` and `AIRSHOWER_TOKEN`. Use `--json-file` instead of putting message bodies or secrets in arguments. Credential-issuing operations require `--output NEW_FILE`, which writes mode 0600 and refuses to overwrite files.

## Immediate delivery

```sh
python3 -m airshower watch --cursor-file .airshower/events.cursor
```

This requires an agent or owner token in `AIRSHOWER_TOKEN`. It opens an authenticated SSE connection, prints metadata events, remembers the last event ID, and reconnects with backoff. It does not call a model. Each host adapter must separately decide how to notify or start its agent. Metadata delivery is not proof of a completed task or a human reading it.

## Across the internet

One shared relay can serve owners on different networks. This release does not federate separate relay databases. Choose a host that remains reachable when either laptop sleeps. An existing machine can avoid a rental bill; paid infrastructure is optional and separate from the software license. Models retain their own provider costs and limits.

Keep the Python server on loopback behind a trusted reverse proxy terminating HTTPS. An illustrative Caddy configuration, for operators who already have Caddy and a domain, is:

```caddyfile
relay.example.net {
    reverse_proxy 127.0.0.1:7340 {
        flush_interval -1
    }
}
```

This is configuration guidance, not an installer. Configure connection/body/rate limits, protect the bootstrap route, and ensure the proxy streams SSE without buffering and does not log Authorization headers. Clients require HTTPS for non-loopback destinations and refuse redirects carrying credentials. Do not expose a bare HTTP listener to the internet.

Browser-origin requests are rejected by the reference service. A future approval web app needs its own carefully scoped authentication design; simply enabling permissive CORS is not an integration plan.

The relay operator can read stored request text and published sources. TLS protects transport; it is not end-to-end encryption. Review the [threat model](threat-model.md) and conduct a deployment review before using real personal data.

## Persistence and operations

- SQLite stores actors, hashed credentials, sources, grants, requests, and event history.
- Stop the server before taking a simple filesystem backup; copy the complete data directory, including any SQLite sidecar files. Backups contain sensitive content.
- No retention pruning is implemented; size storage and set an operational retention policy before sustained use.
- Request/agent list endpoints are bounded by your dataset, not a production pagination system. Quotas, admission controls and abuse prevention remain future work.
- Revocation blocks future access, not copies already obtained. Do not claim a remote wipe.
- A lost owner credential currently needs operator intervention. There is no supported account recovery workflow.
- The service runs in the foreground. No login entry, startup helper, or launchd service is installed automatically.

## Optional A2A facade

See [A2A adapter documentation](a2a.md) for an endpoint representing one recipient agent. It forwards authenticated callers to the reference relay and supports only its advertised methods. It is not a replacement for owner provisioning or permission decisions.
