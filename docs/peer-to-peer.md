# Peer-to-peer wire profile v1

Status: implemented experimental profile in desktop preview 0.3.0. The HTTP
relay and A2A facade remain separate. This P2P profile is not A2A-compatible on
the wire and is not an adopted standard.

## Identity and transport

The implementation pins `@number0/iroh` 1.1.0. ALPN is UTF-8 `airshower/p2p/1`.
A persistent Ed25519 public key identifies each endpoint (64 lowercase hex
characters in this implementation). QUIC verifies possession of the private
key. The remote key must match a locally approved, active contact before
application messages are accepted.

A public connection card contains exactly `protocol`, `name`, `endpoint_id`,
`ticket`. The protocol is `airshower/p2p/1`; the Iroh ticket must identify the
same key. Names are self-asserted. Both owners approve exchanged cards through
a trusted channel. No bearer secret is exchanged. The preview accepts only the
four production relay hosts under `relay.n0.iroh.link`; arbitrary relay URLs
are rejected. N0 discovery can resolve changing addresses. Direct IP routes
remain pinned to the expected public key.

Infrastructure follows [Iroh production defaults](https://github.com/n0-computer/iroh/blob/main/iroh/src/defaults.rs).
It supplies discovery, hole punching and encrypted forwarding, not offline
storage, model execution, application permissions, or universal connectivity.

## Application streams

One bidirectional QUIC stream carries one UTF-8 JSON envelope, delimited by FIN.
Maximum request/response size is 8 MiB. Duplicate keys, excess nesting,
non-finite numbers, malformed fields and extra authority fields are rejected.
Response is `{ok:true,value:...}` or sanitized `{ok:false,code:...}`. Stream
reads, request waits, concurrent connections and application state are bounded.

Every envelope has `version:1` and `type`:

| Type | Other fields | Authority |
| --- | --- | --- |
| submit | request | Sender equals cryptographic peer; recipient is local. |
| get | request_id | Original sender only. |
| cancel | request_id | Original sender, nonterminal request. |
| update | request snapshot | Pinned recipient updates its sender's existing request. |
| catalog | none | Only sources published to this contact. |
| file | request_id, source_id | Original sender, completed handoff, live permission. |

Canonical request fields: `id` (UUIDv4), `sender_agent_id`, `recipient_agent_id`,
`kind`, `message`, `source_ids`, `idempotency_key`, `created_at`, `expires_at`.
Times are integer Unix milliseconds; lifetime is at most 24 hours and creation
allows five minutes of positive clock skew. Kinds: `message`, `ask_human`,
`context`, `file`, `task`. Only context/file requests may have source IDs and
require at least one. Changed replays of an ID or sender idempotency key fail.

The sender's durable outbox retries unknown delivery without creating another
request. Sender-local `queued` differs from recipient-authoritative states:
`pending_approval`, `waiting_human`, `ready`, `working`, `completed`, `denied`,
`cancelled`, `expired`.

Snapshots add `status`, `revision`, `updated_at`; completion adds `result` and
`attribution`. The sender checks scope, peer, monotonic revision, attribution
and attachment metadata. Terminal results cannot change. Result shape:
`{text,source_ids,attachments}`. Each attachment is `{id,filename,size,sha256}`.
Attribution is `{kind,endpoint_id}`: human only for ask_human, otherwise agent.
This trusts the authenticated remote implementation to enforce its owner flow;
it does not independently attest a person's identity.

## Local authorization and delivery

The MCP API accepts no model-supplied approval, human answer, private key, or
arbitrary source path. A native window collects the owner's exact decision.
Review rechecks scope/revision after the prompt; cancellation, expiry or contact
revocation makes an earlier decision inapplicable. Agent completion cannot
answer human questions. Standing rules cover exact kinds/source IDs for one
peer. Reads and file downloads recheck live permissions. Earlier copies cannot
be retracted.

Each MCP process holds a private app profile with an OS file lease. It accepts
streams immediately while alive, pushes dirty recipient updates, and retries
with bounded rotating sync passes. A sender distinguishes acknowledgement,
approval, completion and pending cancellation. Both peers must overlap online.

Remaining scope: shared local daemon/multiple simultaneous processes per
profile, automatic idle-chat wakeup, offline encrypted mailboxes, key rotation,
large-file streaming, archival, production infrastructure, natural-language
policy evaluation and automatic chat connectors. See [desktop behavior and
trust boundaries](p2p-desktop.md) and [verification](p2p-verification.md).
