# Experimental A2A facade

Airshower includes a small **A2A 1.0 HTTP+JSON subset**, not a complete or
certified A2A implementation. It translates requests into Airshower's existing
permission API; it cannot wake a desktop agent by itself. No independent A2A
SDK interoperability or live Codex/Claude/Antigravity session has been verified.

The facade uses the official [A2A 1.0 data model and HTTP binding](https://a2a-protocol.org/latest/specification/)
and an [explicitly negotiated extension](https://a2a-protocol.org/latest/topics/extensions/).
The response, part, card and authentication shapes were also checked against the
[normative protocol definition](https://github.com/a2aproject/A2A/blob/main/specification/a2a.proto).
Those sources were checked on 2026-10-21. A project extension is not an endorsed
or registered extension of the A2A project.

## Run and trust boundary

```python
from airshower.a2a import create_a2a_server

server = create_a2a_server(
    backend_url="http://127.0.0.1:7340",
    recipient_agent_id="YOUR_RECIPIENT_AGENT_ID",
    public_url="http://127.0.0.1:7341",
    port=7341,
)
try:
    server.serve_forever()
finally:
    server.server_close()
```

There is one fixed recipient agent per facade. `public_url` is its advertised
base URL, not the backend URL. An optional path prefix is supported. The Python
entry point returns a `ThreadingHTTPServer`; another thread can stop it with
`shutdown()`, followed by `server_close()`.

No token is configured in the facade. It forwards **the caller's bearer token**
to the fixed backend on each request. Backend permissions decide who may send,
read and cancel. A task addressed to another recipient is hidden even if the
backend would let that caller read it. Owner credentials can read authorized
tasks but cannot create agent requests. Redirects from the backend are never
followed, preventing credential forwarding to a redirected destination.

Configure only a backend you trust with those credentials. HTTP is allowed only
for loopback URLs; use HTTPS and appropriate deployment controls for remote
URLs. This sample HTTP server does not itself terminate TLS or provide public
internet hardening. Browser origins are rejected; CORS is not enabled. Agent
Cards are unsigned and public. Pairing, OAuth, federation, E2EE and public
directory trust are outside this implementation.

## Implemented surface

| Endpoint | Behavior |
| --- | --- |
| `GET /.well-known/agent-card.json` | Public card; also available under the configured base path |
| `POST /message:send` | New independent request; returns `{ "task": ... }` |
| `GET /tasks/{id}` | Returns a `Task` directly, with current authorized state |
| `POST /tasks/{id}:cancel` | Empty JSON object; returns a `Task` directly |

The facade has no listing, streaming, push, extended card, transcript history,
multi-turn task input or cross-task context mapping. Unsupported operations
return explicit errors. The card declares streaming and push as false and lists
the supported subset in extension parameters. An empty history satisfies
`historyLength`; this facade never returns conversation history.

Send must set `configuration.returnImmediately` to `true`. False or omitted is
rejected **before creating a task**, because ordinary A2A's default is a blocking
send. `contextId`, `taskId` and `referenceTaskIds` in a new message are rejected
rather than silently losing continuity. Each task gets a stable derived context.

## Extension contract 0.1

The versioned project identifier is:

`https://github.com/waverace-dev/airshower/blob/main/docs/specification.md#v0.1`

It identifies this project's draft; it is not fetched at runtime. Breaking
profile changes must receive a new identifier/version. Every protected operation
requires these headers:

```http
A2A-Version: 1.0
A2A-Extensions: https://github.com/waverace-dev/airshower/blob/main/docs/specification.md#v0.1
Authorization: Bearer YOUR_CALLER_TOKEN
Content-Type: application/a2a+json
```

Version can alternatively be supplied as the `A2A-Version` query parameter.
Conflicting versions are rejected. A missing version implies legacy 0.3 and is
rejected; the facade never silently treats it as 1.0. Tokens in query parameters
are rejected.

Example message body:

```json
{
  "message": {
    "messageId": "logo-request-001",
    "role": "ROLE_USER",
    "parts": [{"text": "Please send the approved logo.", "mediaType": "text/plain"}],
    "metadata": {
      "https://github.com/waverace-dev/airshower/blob/main/docs/specification.md#v0.1": {
        "version": "0.1",
        "kind": "file",
        "sourceIds": ["SOURCE_ID_SHARED_BY_OWNER"]
      }
    }
  },
  "configuration": {"returnImmediately": true}
}
```

Kinds are `message`, `ask_human`, `context`, `file`, and `task`. Context/file
requests require explicit source IDs; they do not search private chats or a
filesystem. Other kinds cannot request sources. Only one plain text input part
is accepted. File uploads and URL fetching are not accepted through this facade.
If `acceptedOutputModes` is supplied, this subset requires both `text/plain` and
`application/json` because file descriptors are structured data.

The fixed recipient and authenticated caller determine identities. The message
cannot claim approval, replace the recipient, or impersonate a sender. The
backend persists a message-ID-derived idempotency key; matching replay returns
the existing request and a changed translated payload conflicts.

## States, attribution and files

| Airshower state | A2A state |
| --- | --- |
| `ready` | `TASK_STATE_SUBMITTED` |
| `pending_approval`, `waiting_human`, `working` | `TASK_STATE_WORKING` |
| `completed` | `TASK_STATE_COMPLETED` |
| `denied` | `TASK_STATE_REJECTED` |
| `cancelled` | `TASK_STATE_CANCELED` |

Task metadata under the extension URI retains the Airshower status and kind.
Pending approval and human replies add `waitingFor: "owner"`; they do not imply
that the requesting client must supply the recipient owner's decision. The
backend's human/agent attribution is retained when present.

Completed results become artifacts. Plain text uses a text part. Authorized file
descriptors use an `application/json` data part, retaining filename, type, size
and SHA-256 plus a relative `downloadPath` on the **Airshower backend**. The caller
must already know its trusted backend origin and authenticate there normally.
These descriptors contain no file bytes, credentials, automatic downloads or
bearer-token URLs. The backend rechecks access when the file is downloaded;
getting a descriptor is not a permanent access grant.

The facade does not return private sources or approval details. Revocation and
sharing controls remain the backend's responsibility. A completion result that
was already disclosed cannot be retracted from the receiver's memory or copies.

## Verification boundary

`python3 -m unittest discover -s tests -p 'test_a2a.py' -v` exercises HTTP shape,
required negotiation, identity forwarding, recipient isolation, explicit subset
errors, idempotency, cancellation, attribution, owner waits, and safe file
descriptors using a permission-checking fake backend. These tests validate this
facade's contract. An integration test also uses the actual Airshower backend to
verify a human reply, granted file completion, exact downloaded bytes and download
denial after grant revocation. Neither test establishes external SDK conformance
or internet deployment.
