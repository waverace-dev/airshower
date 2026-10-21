# P2P verification evidence

This file records implementation evidence, not desktop certification.

## Local checks, 2026-10-21

- Real Iroh endpoints exchanged encrypted requests and results over an observed
  selected direct path. No transport mock substitutes for these local tests.
- Seventeen focused P2P tests cover bilateral contact pinning, denial, dismissal,
  exact human answers, cancellation during approval, standing-rule revocation,
  scoped context, real file bytes/digests, source revocation, malformed/replayed
  messages, terminal-result immutability, process restart/outbox replay, private
  files, competing process locks, crash recovery, unsafe cards, late network
  completion, desktop MCP schemas, and cancellable waiting.
- All 193 Python reference tests and the older desktop adapter's 39 tests passed after shared MCP plumbing changes.
- The native owner helper compiled on Apple silicon. A fictional standalone
  answer prompt returned a typed human response. This was not a real contact
  permission or a Claude/Codex-hosted exchange. No private answer is published.
- A public-infrastructure fixture connected through n0 discovery/relay routing,
  then reported a selected direct path. This alone does not prove a relayed
  application transfer.

- Public-infrastructure request/reply and restart with the original pinned cards passed on this Mac.
- Both extracted desktop packages passed archive and ad-hoc signature integrity checks, then a real two-process MCP request/approval/result roundtrip using fictional decisions. The Codex package's bundled Node v24.22.1 was used; no host was installed.

## Release gates still recorded separately

`integrations/p2p/tests/internet-smoke.mjs` exercises public request/reply and
restart using original cards. The manually triggered **P2P public relay smoke
test** workflow blocks non-DNS UDP only in its disposable Linux runner and
requires an observed selected relay path. It must pass before claiming forced
relay fallback was verified. Default CI uses isolated local endpoints.

Built archive integrity, extracted runtime/MCP checks, full reference tests and
CI results are recorded in the release notes once run. A passing build is not
proof of host installation, UI quality, or two-person acceptance.

## Not verified

- Installation/loading in real Claude Desktop and Codex through vendor UI.
- A request/reply between two physical Macs, different people and networks.
- Sleep/wake behavior across real networks, production-scale abuse resistance,
  independent security audit, Developer ID signing and notarization.
- Idle-chat wakeup is not implemented. Neither are shared multi-host daemon
  routing or concurrent MCP processes sharing one app profile.

Report app/OS versions and observed outcomes separately. Use fictional requests
for initial testing, and never publish private profiles, keys or real content.
