# Changelog

## 0.3.0 — experimental P2P desktop preview

- Added Iroh encrypted peer transport, local endpoint identities and bilateral
  public-card approval. No Airshower account server is required for this path.
- Added native owner prompts, exact standing permissions, local outbox/retry,
  restart recovery, scoped context and file handoffs with digest verification.
- Added Apple silicon Claude/Codex packages; the Codex ZIP includes Node.
- Added tests for real peer streams, permissions, replay, revocation, crash
  recovery and extracted package interoperability; Mac/Linux CI passed.

Actual vendor installation and the two-person/two-Mac acceptance test remain
unverified. Idle-chat wakeup and shared multi-host routing are not implemented.
Helpers are ad-hoc signed, not notarized. See [verification](docs/p2p-verification.md).
The previous HTTP relay remains a separate plaintext-trusted reference path.

## 0.2.0 — experimental desktop preview

- Preview 2 removes unnecessary reverse DNS from relay startup and improves cold-runner CLI test diagnostics; desktop package bytes are unchanged.
- Added a dependency-free desktop MCP adapter, a reproducible Claude Desktop extension, and a portable Codex plugin with an onboarding skill.
- Added private, restart-safe setup; single-use enrollment and contact invitations; and separate identity, connection, and live-host diagnostics.
- Added owner review through native forms or password-authenticated HTTPS pages, selected by host capabilities. Opening a review page never approves an action.
- Added protocol tests for separate desktop adapter processes, real TLS requests, owner decisions, denied work, and human-attributed answers.
- Published an agent-readable setup entrypoint and privacy/deployment guidance.

This is a preview for active conversations. Actual desktop installation, a two-Mac internet test, idle-chat wakeup, desktop file downloads, federation, and end-to-end encryption remain unverified or unimplemented. No public relay is operated by this project. The experimental wire profile remains `airshower/0.1`.

## 0.1.0 — experimental proposal

- Defined five interaction kinds for people and their agents.
- Added a Python standard-library relay with distinct human/agent authority, contacts, grants, explicit approvals, attribution, context sources, and protected file handoffs.
- Added durable request/event persistence and authenticated SSE reconnects.
- Added a local simulated demonstration, a pinned MCP adapter, and an optional A2A HTTP facade.
- Published permission and transport tests, governance, threat model, and an interoperability roadmap.

This initial version does not claim live desktop integration, internet deployment validation, federation, end-to-end encryption, lab endorsement, or standards-body adoption.
