# Design decisions and open questions

These are decisions for Airshower's experimental 0.1 reference implementation, not settled requirements of a widely adopted standard.

## Accepted for the prototype

1. **People own authority; agents have narrower credentials.** Agents send and process requests. Owners establish contacts, publish sources, authorize sharing, and answer human questions. This makes “ask Morgan” observably different from “ask Morgan's agent.” Attribution records the authenticated authority, not proof of physical human authorship.

2. **Consent is local to the recipient and directional.** An incoming contact approval lets a known person's agents ask; a separate grant or per-request decision controls the work and sources. Approval by A does not silently approve B's reverse access. Exact IDs keep the first policy model understandable and testable.

3. **Source access and disclosure are separate steps.** A recipient agent may retrieve approved context text or inspect file metadata to do work. Its source endpoint never returns file bytes. It must explicitly complete the request before the sender agent or owner can download the completed file IDs through the authenticated download endpoint. Context requests are capped at 1 MiB of total UTF-8 source text even when a grant covers a larger collection. The server rechecks authorization and request limits at each boundary. It cannot retract previously received information or prove that free-form text only uses permitted knowledge.

4. **Publish curated context first.** There is no crawler or unrestricted chat-history connector. Owners deliberately supply excerpts and files. Later connectors must distinguish reading for reasoning, quoting, forwarding transcripts, and transferring files without pretending an LLM can guarantee semantic non-disclosure.

5. **Use one shared server before federation.** Multiple owners can collaborate across networks through a properly hosted server. Server-to-server discovery, identity resolution, conflict handling, and trust negotiation are deferred. The server is trusted with plaintext; TLS is required for internet transit, and end-to-end encryption is unimplemented.

6. **Push notifications are metadata; state remains authoritative.** SSE provides prompt notification, reconnect cursors, and durable replay. Bodies stay behind authenticated request/source APIs. Adapters must handle duplicate delivery and fetch current authorization. An event does not imply a running agent or an attentive person.

7. **Expose an experimental reference API before claiming standard compliance.** The custom `/v1` HTTP endpoints make permission and lifecycle semantics executable. They are not A2A wire endpoints. An [optional A2A facade](a2a.md) explores a limited integration surface; it does not establish general conformance. A consent profile and independent interoperability tests should carry these semantics into the existing ecosystem.

8. **Keep the first runtime dependency-light.** Python's standard library and SQLite make a local experiment inspectable and reproducible without paid services. This is a prototyping choice, not a scalability or security claim. Hosting, agent inference, and operations can still incur costs.

9. **Add an ability to existing agents.** Airshower is a background bridge, not a new everyday chat destination. People express intent in their existing agent conversations; host adapters deliver incoming requests, obtain human decisions when required, and return results to the right conversation. Anyone may become an owner-approved contact. Specific people, meetings, and file exchanges illustrate the general capability; they do not limit its scope. A skill can describe the tools, but runtime delivery and authenticated approvals require actual host integration. Mac integration is the first verification target, with a portable core for later platforms.

## P2P preview update (0.3)

The desktop path now implements Iroh encrypted peer transport with local
identities and bilateral public-card pinning, native owner decisions, scoped
source catalogs, durable retry and file handoff. This is a separate wire
profile, not an A2A endpoint or federation of the earlier HTTP servers.
Decisions 5, 6 and 8 above describe the original HTTP prototype, not the P2P
runtime. See [the profile](peer-to-peer.md), [trust boundaries](p2p-desktop.md)
and [verification](p2p-verification.md). Idle-chat wakeup and shared multi-host
routing remain unresolved.

## Background helper update (0.4)

An optional hidden helper is an accepted delivery path when the original desktop chat cannot be triggered. It receives only approved request/context data, uses the owner’s installed subscription runtime, and remains controllable from the normal chat. It does not inherit arbitrary chat history or authorize external app actions. See [scope and evidence](background-agent.md).

## Open design work

| Question | Needed before a broader claim |
| --- | --- |
| How does “ask this person” select an available agent? | Owner-approved routing and fallback across devices; explicit handling of busy, offline, and unavailable sessions |
| How can a sender discover an asset without knowing its ID? | A consent-aware catalog or agent-mediated search that does not expose private filenames and history |
| How do existing chat sessions receive and act on requests? | Verified adapters for each specific app surface, with clear support for notification, wakeup, contextual reply, and approval |
| How should permission to reason differ from permission to quote? | Data-minimizing execution contexts, enforceable connector scopes, review UX, and honest limits for generated prose |
| What survives consent revocation? | Defined retention/deletion semantics for results, event history, local copies, caches, and backups |
| How are tokens rotated and devices removed? | Credential lifecycle, recovery, session invalidation, and audit design |
| How does the system cross server boundaries? | Identity binding, routing, authenticated federation, delivery guarantees, and cross-server revocation behavior |
| How does private transfer coexist with server routing? | Reviewed end-to-end encryption design, key recovery, recipient verification, and metadata analysis |
| How do tasks avoid runaway agent loops? | Request ancestry, budgets, cancellation propagation, quotas, and explicit delegation limits |
| How do interactions reach people away from their Mac? | Consentful notification channels and mobile/web approval UX; no silent external sends |
| How does this map to A2A and other standards? | A version-pinned profile, extension negotiation, capability descriptions, lifecycle mapping, and interoperability fixtures |
| When should the specification stabilize? | Independent implementations, interoperability reports, outside threat review, and an open change process |

## A useful first interoperability milestone

Two owners on one test server connect different agent clients. One requests a permitted asset, one asks a human question, and one requests a contextual answer from a curated excerpt. Both sides observe attribution and status. Unauthorized sources fail, a revoked file stops downloading, a replay does not duplicate work, and an offline client catches up after reconnecting.

Passing this milestone would demonstrate a bounded reference workflow. It would not establish universal app support, federation, production security, or adoption by a frontier lab.
