<a id="v0.1"></a>

# Airshower protocol proposal 0.1

**Status: experimental design proposal and reference implementation.** This document describes `airshower/0.1`, a candidate protocol for people-owned agent interactions. It is not an adopted global standard, a security audit, or a claim of production readiness. Breaking changes are possible.

The key words **MUST**, **MUST NOT**, **SHOULD**, and **MAY** describe requirements for this reference profile. They do not establish conformance to any other protocol. The HTTP `/v1` interface below is Airshower-specific. The optional experimental A2A facade is documented [separately](a2a.md); a standardized consent/interoperability profile remains future work.

## 1. Purpose and boundary

Airshower connects a requesting agent with another person's agent or with that person. Supported intents are a message, a human question, a request for context, a file handoff, and a delegated task. A person remains the authority for their agents and sources.

Version 0.1 uses one shared, self-hosted server with multiple owners. It does not implement federation between servers, end-to-end encryption, public discovery, an account-recovery system, or a connector into arbitrary existing desktop chats. Context sources are deliberately owner-published excerpts. File sources are deliberately owner-published bytes. Registering a source does not by itself authorize sharing it.

Delivery, agent execution, and human attention are distinct. Event delivery MUST NOT be reported as proof that an agent ran or a person read a request. An agent adapter must remain connected or reconnect to receive events; the server cannot wake a sleeping Mac.

## 2. Identity and authority

| Actor | Credential | Authority |
| --- | --- | --- |
| Bootstrap administrator | Admin bearer token | Create owners |
| Owner | Owner bearer token | Manage own agents, incoming contacts, sources, grants, and human decisions |
| Agent | Agent bearer token | Make requests, process requests addressed to it, and receive results within policy |

The server MUST derive actor identity and role from authentication. A request MUST NOT choose its own sender identity. An agent credential MUST NOT authorize owner decisions or generate human attribution. Owner credentials MUST be kept out of agent tool configuration.

Bearer secrets MUST be generated randomly and stored as hashes. Clients MUST send them in the `Authorization: Bearer …` header, never in a URL. The server MUST reject query-token authentication. Credentials are capabilities; disclosure of an owner token compromises that owner's authority.

An incoming contact approval is directional: when owner B adds owner A, B permits requests from A's agents. It does not permit requests in the reverse direction and it does not authorize disclosure of a source. Requests between different owners MUST require recipient-side incoming contact approval. Same-owner requests do not require a contact entry.

## 3. Sources and consent

Sources belong to an owner and have an opaque ID, a label, and a kind of `context` or `file`. A context source contains a curated string; a file source contains bytes and file metadata. API clients MUST NOT supply filesystem paths for the server to read. File uploads MUST use a display filename; the server MUST reject filenames containing paths, slashes, backslashes, or control characters. Uploaded bytes MUST NOT be executed.

Owners MUST explicitly publish each source through the API. The implementation MUST NOT infer access to all of an owner's chats, files, or projects. Source-list endpoints MUST return metadata only and MUST be owner-scoped. Invalid, foreign, or inaccessible source references MUST NOT reveal source labels or contents in errors.

An automatic grant binds one sender agent, one recipient agent, allowed actions, an exact set of source IDs, and optionally an expiry. The recipient agent and all granted sources MUST belong to the granting owner. A cross-owner grant MUST require an approved incoming contact. `context` and `file` grants MUST contain sources of the corresponding kind.

A single active grant MUST cover the requested action and the entire requested source set for automatic approval. Partial permissions from several grants MUST NOT be combined. A grant MUST NOT automatically approve `ask_human`.

Otherwise, the recipient owner may approve or deny the exact pending request. Approval MUST apply only to that request, action, and source set. It MUST NOT create an implicit grant for subsequent requests.

The server MUST recheck current authority before claim, source access, completion, and file download. Removing a grant, contact, or source MUST prevent subsequent access that depended on it. Contact removal MUST deny affected nonterminal requests, revoke affected grants, and invalidate prior request approval bases, including for completed file downloads. Re-adding the contact MUST NOT resurrect those approvals. An explicit request approval is independent of unrelated grants, but still requires current contact and source access.

Revocation is prospective. It cannot erase bytes already downloaded, excerpts already read, or knowledge already incorporated into an agent's state. Request history and completed text may persist after revocation; clients MUST NOT describe revocation as remote deletion. Software-enforced source IDs do not guarantee that generated prose contains no information learned elsewhere. See [the threat model](threat-model.md).

## 4. Request format

`POST /v1/requests` accepts a JSON object validated against [the create-request schema](../schemas/request.schema.json):

```json
{
  "recipient_agent_id": "agent-recipient",
  "kind": "file",
  "message": "Please send the approved transparent logo.",
  "source_ids": ["source-approved-logo"],
  "idempotency_key": "logo-request-001"
}
```

Kinds are `message`, `ask_human`, `context`, `file`, and `task`. `context` and `file` MUST name a nonempty set of existing sources of the appropriate kind owned by the recipient owner. Other kinds MUST use an empty `source_ids` array. The source IDs are selectors, not permission. This version requires a known source ID; semantic discovery of “the final logo” is an adapter/product concern, not an implemented unrestricted search operation.

An idempotency key is scoped to the authenticated sender. Repeating a key with the exact original payload MUST return the existing request without duplicating work. Reusing it with a different payload MUST return `409 Conflict`. An idempotent retry MUST NOT reset the original request state.

Request representations contain `id`, `sender_agent_id`, `recipient_agent_id`, `kind`, `message`, `source_ids`, `status`, `created_at`, and `updated_at`, plus optional `result`, `attribution`, and `reason`. The server MUST keep its approval basis private. Request content MUST be visible only to the sender agent, recipient agent, and their owners.

## 5. Lifecycle

| Current state | Operation or condition | Next state | Authorized actor |
| --- | --- | --- | --- |
| Creation | `ask_human` | `waiting_human` | Sender agent |
| Creation | Other kind with a fully matching active grant | `ready` | Sender agent |
| Creation | Other kind without that grant | `pending_approval` | Sender agent |
| `pending_approval` | Approve exact request | `ready` | Recipient owner |
| `pending_approval` | Deny | `denied` | Recipient owner |
| `ready` | Claim | `working` | Recipient agent |
| `ready` or `working` | Complete non-human request | `completed` | Recipient agent |
| `waiting_human` | Complete human answer | `completed` | Recipient owner |
| Any nonterminal state | Cancel | `cancelled` | Sender agent or sender owner |
| Any nonterminal cross-owner request | Remove sender's incoming contact approval | `denied` | Recipient owner |

`completed`, `denied`, and `cancelled` are terminal. Invalid transitions MUST fail without performing the requested transition. A failed authority check MUST prevent the operation; a stored `ready` or `completed` label is not enduring authorization.

`ask_human` MUST enter `waiting_human` even when other grants exist. Only the recipient owner's authenticated completion can produce `attribution: {"type": "human", "id": …}`. The server MUST derive attribution; agent completions receive agent attribution. The reference implementation does not prove that a biological human physically typed the response—human attribution establishes that the owner credential authorized it.

Completion requires `text` and `source_ids`. Completed source IDs MUST be a subset of the original request; file completion MUST include at least one file ID. While `ready` or `working`, the recipient agent's source endpoint returns approved context text and file metadata only, never file bytes. Source access MUST NOT by itself complete the request or release a file to the sender. Result objects contain text and, where relevant, file metadata, never source bytes automatically. A context answer may intentionally include information from approved excerpts.

A completed file request permits the sender agent or sender owner to download only the explicitly completed file IDs. This authenticated download endpoint is the only API that returns file bytes. Download MUST recheck current authority and source existence. Responses MUST use safe attachment filenames and MUST NOT expose local filesystem paths. A successful HTTP download confirms a transfer response, not that a receiving application safely opened or understood the file.

## 6. HTTP reference interface

All paths except `/health` require bearer authentication. JSON request and response bodies use UTF-8. Times are Unix timestamps. The default server bind MUST be `127.0.0.1`. Internet deployment requires an authenticated, correctly configured HTTPS deployment; this prototype supplies neither public hosting nor a production hardening guarantee.

| Method and path | Purpose / permitted actor |
| --- | --- |
| `GET /health` | Public health, version, and protocol identifier |
| `GET /v1/me` | Authenticated identity |
| `POST /v1/owners` | Admin creates owner; returns owner token once |
| `POST /v1/agents` | Owner creates owned agent; returns agent token once |
| `GET /v1/agents` | Owner's own agents; an agent also sees agents of owners accepting its owner's incoming requests |
| `POST /v1/contacts`, `GET /v1/contacts` | Owner approves or lists incoming contacts |
| `DELETE /v1/contacts/{owner_id}` | Owner revokes incoming contact |
| `POST /v1/sources`, `GET /v1/sources` | Owner publishes sources or lists own source metadata |
| `DELETE /v1/sources/{source_id}` | Owner removes source accessibility |
| `POST /v1/grants`, `GET /v1/grants` | Owner creates or lists own grants |
| `DELETE /v1/grants/{grant_id}` | Owner revokes grant |
| `POST /v1/requests` | Agent creates request |
| `GET /v1/requests`, `GET /v1/requests/{request_id}` | Participants and their owners read scoped requests |
| `POST /v1/requests/{request_id}/decision` | Recipient owner submits `approve` or `deny` |
| `POST /v1/requests/{request_id}/claim` | Recipient agent claims authorized request |
| `GET /v1/requests/{request_id}/sources` | Recipient agent reads approved context text or file metadata only while `ready` or `working` |
| `POST /v1/requests/{request_id}/complete` | Recipient agent completes work, or recipient owner answers `ask_human` |
| `GET /v1/requests/{request_id}/files/{source_id}` | Sender agent or owner downloads an authorized completed file |
| `POST /v1/requests/{request_id}/cancel` | Sender agent or owner cancels nonterminal request |
| `GET /v1/events` | Authenticated, actor-filtered SSE stream |
| `GET /v1/events/history` | Authenticated, actor-filtered event history |

The server MUST enforce a maximum of 5 MiB per file, 64 KiB per context source, and 16 KiB per prompt or result text. A context request MUST select no more than 1 MiB of total source text measured in UTF-8 bytes, checked at creation, owner approval, and subsequent authority checks. Grants MAY cover larger collections, but each request MUST select a subset within that limit. Request identifiers and idempotency keys MUST be nonblank and at most 128 UTF-8 bytes; source lists MUST contain at most 100 unique IDs. Request messages MUST be nonblank; completed result text MAY be empty. JSON Schema character limits do not replace UTF-8 byte checks. Base64 transfer encoding is not the file's content size. The server MUST bound HTTP bodies before reading them, reject unsupported chunked requests, and not enable cross-origin access by default.

Errors use `{"error":{"code":"…","message":"…"}}`. Relevant statuses are `400` invalid input, `401` failed authentication, `403` forbidden action, `404` absent or out-of-scope object, and `409` conflicting state or idempotency key. Unexpected errors MUST be sanitized; responses MUST NOT disclose tracebacks, secrets, or private source material.

## 7. Events and reconnection

SSE events notify the client that request state changed. Their data contains only:

```json
{
  "id": 42,
  "type": "request.completed",
  "request_id": "request-example",
  "status": "completed",
  "created_at": 1792530000
}
```

The example `type` illustrates a request lifecycle notification; consumers SHOULD use the request's current representation as authoritative. Event data MUST NOT contain prompts, source bodies, file bytes, credentials, or result text. Metadata is still sensitive.

The server MUST persist monotonically increasing event IDs and filter delivery by the authenticated actor; an owner can see events for their owned agents. IDs may have gaps because other actors' events are filtered out. Consumers MUST NOT infer hidden event contents from those gaps.

Clients MAY resume with `?after=N` or the SSE `Last-Event-ID` header. The server MUST replay accessible events after the cursor in bounded batches and send heartbeat comments while idle. The JSON history endpoint provides the same scoped metadata for clients that cannot keep an SSE connection. It is not the primary push transport.

Clients SHOULD persist their last processed cursor, tolerate replay, and reconnect with backoff. They MUST NOT treat an event as permission to act: fetch current state and perform the normal authorized operation. Persistence supports offline replay after reconnect and restart; it does not create an offline-running agent, deliver a mobile notification, or guarantee response latency.

## 8. Conformance scope and future interoperability

An implementation claiming this draft reference profile MUST pass identity, participant isolation, source authorization, lifecycle, idempotency, revocation, attribution, file-transfer, and event-replay checks. A JSON Schema pass alone is insufficient: ownership, contact approval, source kind, and live grants require server-side state checks.

The planned interoperability work is an A2A profile for these person-owned consent and attribution semantics, plus adapters exposing narrowly scoped tools to existing agents. The optional facade's supported subset and limits are recorded in [A2A adapter documentation](a2a.md). General A2A conformance, broad desktop-agent support, federation, and cross-vendor adoption are not claimed by version 0.1. Proposed changes and unresolved tradeoffs are recorded in [decisions](decisions.md).
