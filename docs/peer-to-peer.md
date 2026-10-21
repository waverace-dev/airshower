# Peer-to-peer transport proposal

Status: research proposal, checked 2026-10-21. The 0.2 desktop preview still uses
the existing shared application relay. It is not peer-to-peer or end-to-end
encrypted. No new networking library or service has been installed or enabled.

## Proposed experience

Each Mac keeps its own identity, contact approvals, permissions, and request
history. Contacts pair through an authenticated invitation. The local bridge
connects directly to the other person's bridge when possible, with encrypted
relay fallback for networks that prevent a direct connection. Existing agent
chats remain the interface. Nobody should need to configure a router or use a
terminal.

Public infrastructure may help devices find and reach each other without
holding application plaintext. This is distinct from the current application
relay, which stores and authorizes requests and can read their content. A peer
transport must preserve recipient-owned authorization, authenticated human
answers, replay protection, cancellation, and revocation; encryption alone
does not supply these properties.

## Candidate to evaluate first

[Iroh](https://github.com/n0-computer/iroh) offers public-key-addressed QUIC,
NAT traversal, and relay fallback under MIT or Apache-2.0. Its
[relayed traffic remains encrypted between endpoints](https://docs.iroh.computer/concepts/security-privacy).
Relays can observe connection metadata; direct peers learn each other's IP
addresses. We should not describe this as anonymity.

Its [public development relays](https://docs.iroh.computer/iroh-services/relays/public)
are free for development and hobby use, with rate limits, no uptime guarantee,
and support only for the latest stable release. This could avoid paid hosting
for an initial test using harmless fixtures. It is not a promise of free
production infrastructure.

The [Node bindings](https://docs.iroh.computer/languages/javascript) require
Node 20.3+ and currently provide macOS binaries for Apple silicon only.
The present Airshower package supports Node 18+ and has no native dependencies,
so this is not a drop-in change. A bundled native helper is another option to
evaluate, including Intel support, signing, packaging, updates, and startup.
[libp2p](https://docs.libp2p.io/concepts/hole-punching) is an alternative with a
broader modular networking stack; transport support must be verified for the
chosen language and release.

## Evidence required before switching

- Pair two real Macs on different networks, verify their keys, and report
  whether the connection is direct or relayed. Test both paths deliberately.
- Keep a durable sender outbox and retry after sleep/reconnect. An offline Mac
  cannot receive or run an agent; queued, delivered, approved, and completed
  must remain distinct. Encrypted store-and-forward is a separate feature.
- Retain trusted owner decisions locally. The inspected Claude Desktop build
  offers HTTPS URL review rather than forms. P2P does not remove that UI
  constraint: investigate a supported native owner prompt or an authenticated
  HTTPS approval design. Do not bypass certificate checks or put authority in
  a review URL.
- Verify receipt separately from agent execution. A background bridge can
  receive bytes; waking an idle desktop conversation still requires a supported
  host integration.
- Test revocation, duplicate delivery, expiry, process restart, key loss, and
  forbidden source access. Reuse the current lifecycle tests where applicable.

The standard should describe interoperable identity and permission semantics
without requiring one vendor's transport or relay service.
