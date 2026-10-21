# Existing work and the hypothesis we are testing

Research snapshot: 21 October 2026. These are source-document findings, not hands-on validation of other products. A feature not verified here is not necessarily absent.

## We do not need a new base messaging protocol

- **[A2A](https://a2a-protocol.org/latest/)** already specifies interoperable agent task exchange, discovery, results, and asynchronous interactions. Its [extension mechanism](https://a2a-protocol.org/latest/topics/extensions/) is the intended place to explore additional profile semantics. The reference facade currently implements a declared subset; [A2A notes](a2a.md) enumerate it.
- **[MCP](https://modelcontextprotocol.io/specification/2025-11-25)** connects assistants to tools. It is useful for sending and retrieving requests; it does not itself guarantee that an idle desktop conversation wakes up.
- **[Agent Relay](https://github.com/AgentWorkforce/relay)** and **[Relaycast](https://github.com/AgentWorkforce/relaycast)** already provide cross-machine messaging, channels, files, delivery, and adapters. They are the closest existing foundations identified so far. Both repositories publish Apache-2.0 licenses. [Relay Connect](https://agentrelay.com/docs/relay-connect) explicitly targets people connecting their existing Codex or Claude Code chats through temporary rooms.
- **[Claude Relay](https://github.com/gvorwaller/claude-relay)** documents cross-machine communication over SSH/WebSockets, MCP tools, and Codex/Antigravity delegate wakeups. Its README declares MIT; license coverage should be clarified before incorporating code. No code from it is included here.

## The product hypothesis

People may benefit from one consistent experience across assistants: choose a contact, ask the person or their agent, grant a specific source/action scope, and receive an attributed answer or file in the originating conversation.

The differentiating hypothesis is the complete permission and human-involvement experience. We have not established that existing projects cannot deliver it. A2A task transport, MCP tools, messaging, and file transfer themselves are not novel claims.

## Comparative evaluation still needed

Test the same flow in Airshower and the nearest existing solution:

1. Two people, different machines and accounts; pair without sharing provider credentials.
2. An existing agent asks another person's existing agent for a file.
3. That person approves exactly one source, without approving unrelated chats.
4. Their agent returns the correct bytes with a known version and attribution.
5. The original conversation notices the reply while idle, without manual inbox polling.
6. A human question reaches the human and the human answer is distinguishable from an agent's answer.
7. Disconnect, reconnect, retry and revoke without duplicate execution or unauthorized disclosure.

Record installation steps, runtime versions, host APIs, latency, grant scope, failure modes, and operating costs. No performance or competitive superiority claims should be published before these comparisons.

## Reuse policy

Airshower's initial reference code is a small independent implementation so that its permission rules can be inspected and tested without hosted-service accounts or dependencies. This is not a rejection of the existing engines. A Relaycast-backed implementation or other transport can be considered once it preserves the same observable policy behavior.

Keep the profile implementable independently. Document attribution and license terms for any code later reused. Never add license restrictions excluding specific model providers.
