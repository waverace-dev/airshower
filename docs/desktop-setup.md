# Desktop preview

> This document covers the earlier HTTP relay preview. For the 0.3 P2P release, see [P2P desktop setup and privacy](p2p-desktop.md).

The intended everyday interface is the agent conversation you already use. This preview supplies a Claude Desktop extension and a Codex plugin, with a dependency-free Node adapter shared between them. It has no separate chat app.

Download the packages and checksums from the [0.2 desktop preview release](https://github.com/waverace-dev/airshower/releases/tag/v0.2.0-preview.2). They currently require a shared relay; [peer-to-peer transport](peer-to-peer.md) is a proposal, not a feature of these packages.

## Claude Desktop

Install the `airshower-0.2.0.mcpb` package through Claude Desktop's extension settings. Claude's [MCPB integration](https://claude.com/docs/connectors/building/mcpb) provides the runtime. Installation and entering its settings are graphical steps; no terminal is required.

The extension asks for your display name, HTTPS relay address, private invitation, and a new approval password. The invitation is single-use and should arrive privately from the chosen relay operator. The password protects browser approvals; use a new password and keep it outside conversation text. The preview does not provide password recovery or rotation.

Ask Claude: **“Get Airshower ready.”** The `ready` tool enrolls the profile if needed and verifies the dedicated agent identity. Then ask to connect the invited contact. Contact acceptance and request review use a native elicitation request. On hosts with URL elicitation, that opens the relay's HTTPS approval page, where you authenticate and review the exact request. Merely opening the page does not approve anything.

Form elicitation is supported when a host advertises it. The locally inspected Claude Desktop 3.1623.0 advertises URL elicitation and declines form elicitation; the adapter therefore needs the HTTPS approval path for that desktop version. Capability negotiation, not the app's name, selects the path. A host without a supported approval path leaves the decision pending.

## Codex desktop

The `airshower-codex-0.2.0.zip` package contains the portable plugin manifest, the shared Node server, and the Airshower onboarding skill. It is a local package, not an approved store listing. [Codex packaging and installation details](../integrations/codex/README.md) describe the supported local marketplace route.

An existing Node runtime is required for this package. The setup agent can use a user-selected private invitation file or an existing private profile; it must keep credentials out of model inputs, command arguments, and generated host configuration. An invitation's relay must match the connection address the person intends to use. No provider account credentials are exchanged.

## First real test

Keep both conversations active for the first test:

1. Each person asks their agent to get Airshower ready. Confirm authenticated identity and the intended contact.
2. The sender asks their agent to ask the contact a harmless question through Airshower.
3. The recipient asks their agent to check Airshower requests. It opens the pending request and asks the owner through the supported approval flow.
4. The recipient answers or declines. The sender's agent checks or waits for the result and reports its attribution.
5. Repeat with a task requiring approval: denial must prevent the recipient agent from claiming it; approval permits processing but is not proof that an external action occurred.

The protocol tests exercise separate adapter processes, actual TLS relay requests, and simulated host decisions. They do not prove that either desktop app has loaded the extension, that a person saw the native prompt, or that two physical Macs have completed a request. Record those observations separately.

## What remains

An ordinary local MCP connection does not automatically start an idle desktop conversation. The preview supports an inbox and bounded waits during active work; it does not provide continuous autonomous desktop reception. The optional Claude Code channel adapter is a separate experimental developer integration and does not establish Claude Desktop support.

The Node desktop adapter supports requests, context results, and file source metadata. It does not yet save received file bytes locally. The existing Python adapter has a protected download tool; desktop parity still needs work. Cross-server federation, end-to-end encryption, independent security review, and directory distribution also remain open.

## Build and inspect (maintainers)

```sh
python3 scripts/build_desktop.py
```

The deterministic builder uses an explicit source allowlist and emits packages plus `SHA256SUMS` under `dist/`. It never packages a profile, invitation, token, user configuration, or development cache. Installation and real-host verification are separate from this build step.
