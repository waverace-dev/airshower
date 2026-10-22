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

## Direct and relay paths

- The implementation CI passed all eight jobs on macOS and Linux:
  [run 39638334983](https://github.com/waverace-dev/airshower/actions/runs/39638334983).
  This includes the Mac package build and extracted two-process check.
- The opt-in public-infrastructure fixture passed on this Mac with a selected
  direct path, and again with a **selected relay path** while test-only code
  discarded 361 non-DNS UDP sends inside the fixture process. Both runs passed
  request/reply and restart recovery using the original cards. No private
  content or real owner permissions were used.
- The isolated packet-drop fixture is `integrations/p2p/tests/drop-udp.c`.
  It is excluded from release archives. It affects only the explicitly launched
  test process; it changes no machine-wide firewall or persistent settings.
  The manually triggered P2P public relay smoke workflow uses the same approach
  on its disposable Linux runner and asserts the selected relay path.
- Earlier CI attempts used machine-wide UDP firewall rules and failed/hung
  during network setup. Those diagnostic runs were cancelled and are not
  evidence of a working fallback. The isolated Mac test above is the completed
  forced-relay evidence; subsequent CI results are linked in release notes.

To reproduce the isolated relay test on an Apple silicon development machine:

```sh
cc -dynamiclib -O2 -Wall -Wextra -Werror integrations/p2p/tests/drop-udp.c -o build/drop-udp.dylib
DYLD_INSERT_LIBRARIES="$PWD/build/drop-udp.dylib" node integrations/p2p/tests/internet-smoke.mjs --require-relay
```

The output must report `require_relay:true`, a selected `relay` path, successful
request/reply and restart, plus a positive discarded-datagram count. A successful
connection alone does not prove relay fallback. A native-helper `--version`
check proves executable integrity, not interactive UI quality or installation.

## Background helper, 2026-10-22

A real Codex subscription run responded to an incoming encrypted local peer request from an idle helper in approximately 5.5 seconds, without a recipient inbox call. Claude’s runtime logged in and launched, but its provider returned a usage-limit error; a successful Claude model response remains unverified. See [background verification and bounds](background-agent.md).

## Not verified

- Installation/loading in real Claude Desktop and Codex through vendor UI.
- A request/reply between two physical Macs, different people and networks.
- Sleep/wake behavior across real networks, production-scale abuse resistance,
  independent security audit, Developer ID signing and notarization.
- Idle-chat wakeup is not implemented. Neither are shared multi-host daemon
  routing or concurrent MCP processes sharing one app profile.

Report app/OS versions and observed outcomes separately. Use fictional requests
for initial testing, and never publish private profiles, keys or real content.
