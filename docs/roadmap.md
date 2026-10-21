# Roadmap and adoption criteria

The goal is a useful interoperable experience. Adoption is earned through working integrations and evidence; no vendor commitments are assumed.

## 0.1: reviewable starting point

- Open profile and clear actor/source/action semantics.
- Working reference relay, fixture demo, and negative authorization tests.
- Explicit MCP protocol pin and optional A2A facade.
- Public limitations, threat model, and contribution path.

## Next: two real people, two real computers

First validate one complete flow with Codex and Claude Code, then Antigravity. Desktop Claude, Claude Code CLI, Codex Desktop, and Codex CLI are distinct integration targets. Do not extrapolate one result to another.

Acceptance criteria:

- Invite and authenticate the second owner without exchanging AI-provider credentials.
- Register an exact agent/session identity; verify liveness separately from tool availability.
- Receive a request through internet connectivity while the app is idle.
- Ask the human, record the answer, and deliver it to the original chat.
- Share a selected file and verify bytes and provenance on the other computer.
- Block a disallowed chat, file, recipient, or action at the runtime boundary.
- Demonstrate duplicate suppression, offline replay, expiry and revocation.
- Measure event delivery and agent reaction separately; report p50/p95 and the test setup.

The reference demo is not evidence for these live-host acceptance criteria.

## Make it approachable

Build a small contact/inbox/approval interface. Use a calm dark-navy and mint palette, high contrast, readable typography, and restrained motion. Show author, purpose, source scope and status in plain language. Protocol internals belong in diagnostics, not the consent prompt. Test the flow with people who do not use terminals.

## Make it independently implementable

- Resolve the A2A extension design with other implementers.
- Publish machine-readable task/artifact/envelope schemas and a portable conformance suite.
- Build a second implementation with an independently maintained client.
- Add least-privilege delegated credentials, secure pairing, quotas, and credential rotation.
- Design selective chat connectors with explicit source-use versus disclosure permissions.
- Evaluate end-to-end encryption and owner-hosted gateways; avoid claiming relay privacy before solving key management.
- Specify federation only after the single-relay model and cross-owner consent are reliable.

## Before a stable standard claim

Require two independent implementations, published interoperability results, external security review, documented migration/version negotiation, and a contributor governance process. A GitHub star count is not a substitute for any of these.

## Release and outreach

Publish a small reproducible demo and invite adapter contributors. Share the exact pain point and evidence. Ask A2A/MCP maintainers whether an extension is appropriate before proposing a competing protocol. Approach labs through their public developer/community channels with working integrations and a narrow request. Outreach drafts are in [launch kit](launch-kit.md); nothing in that file implies messages were sent.
