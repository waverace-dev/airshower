# Threat model — experimental 0.1

> This document covers the earlier HTTP relay preview. For the 0.3 P2P release, see [P2P desktop setup and privacy](p2p-desktop.md).

Airshower's prototype enforces identity and resource scopes in server code. It has not received an independent security audit and is not a production service. Its primary boundary is between owners and between owner and agent credentials on **one trusted server**.

## Assets and trust boundaries

Assets include bearer credentials, owner/contact identities, curated context, uploaded files, request and result text, consent records, and event metadata. Protecting an owner's information requires protecting their device, credential files, the server host, database, backups, and any reverse proxy.

The relay can read message and file contents. HTTP on loopback is the local development default; internet deployment needs TLS. TLS protects a network hop, not data from the server operator. **There is no end-to-end encryption.** Database contents are not claimed to be encrypted at rest.

Owner and agent credentials are separate. Owner credentials authorize contact/source/grant management and human responses. Agent tools must use only agent credentials. An agent-controlled process with access to an owner token can bypass this separation; merely naming a token “owner” is not an OS-level sandbox.

## Threats and controls

| Threat | Implemented boundary or design requirement | Remaining limit |
| --- | --- | --- |
| Agent approves its own access or impersonates a person | Owner-only decision APIs; sender and attribution derived from authenticated role | Stolen owner credentials or automation deliberately using them retain owner authority |
| Stranger sends unsolicited work | Recipient owner must approve incoming contact; approval is directional | An approved contact can still send unwanted requests; no public-service abuse prevention claim |
| Guessing another owner's request or source ID | Participant and ownership checks; scoped missing responses; source-safe errors | Metadata shared intentionally with participants remains visible |
| Approval exceeds intended scope | Exact agents, actions, and source IDs; one grant must cover the whole request; exact one-request approvals | People can grant excessively broad permissions or publish sensitive excerpts |
| Permission is revoked after request creation | Recheck before claim, source read, completion, and download | Already read data, downloaded bytes, and retained result text cannot be recalled |
| Replayed create request causes duplicate work | Sender-scoped idempotency key; payload mismatch rejected | A caller using a new key creates a new request; adapters still need replay-safe event handling |
| Events leak private message bodies | Actor-filtered streams contain state metadata only | Timing, IDs, and the existence of requests are metadata; global ID gaps can reveal aggregate activity |
| File upload reads arbitrary host files or runs code | Source API accepts bytes, no local paths; filenames containing paths or control characters are rejected; safe attachment headers; uploads are not executed | No malware scanning or safe document rendering; receiving tools can expose themselves by opening files |
| Oversized payload exhausts resources | Body and per-field limits; 1 MiB aggregate context limit per request; source reads return file metadata only; bounded event replay | Not a complete denial-of-service defense; connection limits, quotas, rate limits, and operational controls need further work |
| Token disclosure in URL or storage | Authorization header; random tokens; database holds hashes | A bearer token can be replayed by its holder; token rotation/recovery and secret distribution need product work |
| Server compromise or malicious host | Treat host as trusted; deploy with restricted access and TLS | Host can read/alter data and impersonate interactions; no cryptographic protection from the host |
| Malicious request or source prompt-injects an agent | Treat all received text and files as untrusted data; local tool approvals remain necessary | The relay does not sandbox models or neutralize prompt injection |

## Context is deliberately curated

Version 0.1 has no live chat-history connector. An owner publishes a selected excerpt or file. The recipient agent can read approved context text and inspect approved file metadata; file bytes are available only to the sender agent or owner through an authorized completed-request download. This is narrower than granting an agent access to an entire personal assistant history.

The server cannot prove the semantic origin of a free-form answer. An agent may already know a secret from its host session and include it in result text. Source restrictions prevent unauthorized retrieval through this server; they **do not guarantee that generated text contains no information from other contexts**. Adapters should use isolated request sessions with only permitted excerpts, keep owner credentials unavailable, and require human review for sensitive disclosures. These mitigations are design guidance, not a verified isolation guarantee of the prototype.

“Human attribution” means an owner credential submitted the response. It does not verify physical presence, biometric identity, authorship of every word, or the absence of owner-side automation.

## Revocation and retention

Deleting a contact, grant, or source changes authorization for future operations that rely on it. Contact removal denies affected nonterminal requests and invalidates existing approvals, so re-adding a contact does not restore old file access. A terminal request state is not an enduring file-download entitlement. Requests and events persist for continuity; source removal clears the active source content but does not purge history or guarantee erasure from SQLite storage, backups, and earlier copies. A previously completed text answer can remain in history, and a recipient may retain a local copy of any previously received data.

Avoid promises such as “revoke to delete everywhere” or “permission-protected AI answers.” Production design needs explicit retention policies, export/deletion procedures, backup handling, cryptographic identity, token lifecycle management, and tests against hostile multi-tenant traffic.

## Safe scope for experimentation

Use local dummy accounts and nonsensitive sample material first. Keep owner tokens separate from agent environments. For internet trials, use a private, access-controlled deployment with HTTPS and a trusted operator. Never expose the development HTTP listener directly as a hardened public service.

The test suite is evidence of particular implemented checks; it is not a security audit. Please report suspected vulnerabilities using the repository's security reporting guidance rather than posting working credentials or private data in an issue.
