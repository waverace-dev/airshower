# Roadmap and adoption criteria

The goal is an invisible capability inside the agents people already use: express an intent in an existing chat, connect with another person's agent, and receive the outcome in that chat. Anyone can be an owner-approved contact. Dates, files, and questions are examples of a general bridge, not separate products. Adoption is earned through working integrations and evidence; no vendor commitments are assumed.

## 0.1: reviewable starting point

- Open profile and clear actor/source/action semantics.
- Working reference relay, fixture demo, and negative authorization tests.
- Explicit MCP protocol pin and optional A2A facade.
- Public limitations, threat model, and contribution path.

## Next: existing agent chats on two Macs

Start on Macs so the first users can test the complete experience on their own computers. Keep the bridge core portable; an Omarchy/Linux integration follows once the Mac flow works. First validate one complete flow with normal Codex desktop and Claude Desktop, then Antigravity. End users must not need a terminal. Desktop Claude, Claude Code CLI, Codex desktop, and Codex CLI are distinct integration targets. Do not extrapolate one result to another.

The 0.2 preview supplies a Claude extension, a Codex plugin, private invitations, and capability-negotiated owner review. Separate adapter processes have passed request/reply tests against an HTTPS relay with simulated desktop hosts. Installing the packages in the actual apps and completing a two-Mac test remain the next evidence gates. Active-chat operation is the first milestone; background reception remains an unresolved host integration requirement.

Acceptance criteria:

- Invite and authenticate the second owner without exchanging AI-provider credentials.
- Add and remove contacts through an owner-authorized flow; route requests to each recipient's designated agent without hardcoded people or providers.
- Initiate a request from an ordinary conversation with the existing agent, without opening an Airshower chat app.
- Register an exact agent/session identity; verify liveness separately from tool availability.
- Receive a request through internet connectivity while the app is idle.
- Apply the recipient's rules independently of the sender's wishes. For an action requiring human consent, ask through the recipient's supported host interface, record an authenticated decision, and continue only after approval.
- Deliver the result to the original chat, distinguishing completion, refusal, a counterproposal, waiting, and unavailability. These outcomes need a defined mapping; they are not all distinct protocol states today.
- Share a selected file and verify bytes and provenance on the other computer.
- Block a disallowed chat, file, recipient, or action at the runtime boundary.
- Demonstrate duplicate suppression, offline replay, expiry and revocation.
- Measure event delivery and agent reaction separately; report p50/p95 and the test setup.

The reference demo is not evidence for these live-host acceptance criteria.

Evaluate the [peer-to-peer transport proposal](peer-to-peer.md) before choosing
production hosting. The present shared relay is a reference implementation,
not a requirement that every user's content pass through an Airshower service.

## Keep the bridge out of the way

Expose narrowly scoped tools to existing agents, with optional skill instructions that explain how to use them. A background adapter handles delivery, reconnects, and supported host wakeup. Return results and human questions to the relevant existing conversations. A standalone messenger, contact feed, or conversation inbox is not the primary product.

Use the host's trusted approval mechanism where it can authenticate the owner's decision separately from model output. Owner credentials must stay outside model-facing tools. If a host cannot support that boundary or receive requests, report the limitation explicitly; tool availability alone is not a complete integration.

Keep any necessary setup, contact permissions, and diagnostics minimal. Consent prompts should identify the requester, purpose, scope, and consequence in plain language. Match the host's interface; any Airshower-owned setup surface should retain the project's navy and mint palette. Test the flow with people who do not use terminals.

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
