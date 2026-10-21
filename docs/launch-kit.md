# Launch kit — drafts, not sent

## One sentence

Airshower is an open proposal for your agent to coordinate with someone else's agent—with permission, context, and a human involved when needed.

## Short announcement

I want to ask my agent: “Get the final asset from my collaborator,” and have it reach their agent, ask permission if needed, and bring the right file back.

Airshower is an experimental open-source profile and reference bridge for that interaction: messages, human questions, selected context, file handoffs, and delegated tasks. It includes separate human/agent authority, a permissioned local demo, an MCP adapter, and a small A2A facade.

It is early. Existing desktop chat wake-up and real two-Mac interoperability still need verification. We are looking for adapter builders and independent implementations, not claiming a new adopted standard.

Code and runnable demo: https://github.com/waverace-dev/airshower

## Developer discussion starter

How should independent assistants represent the difference between “ask this person,” “ask their agent using approved context,” and “send me a file they permit sharing”?

Airshower proposes explicit owner/agent attribution and source/action grants around those requests. We'd value feedback on mapping this into an A2A extension and implementing host-specific wake-up behavior without pretending MCP notifications alone wake a conversation.

## 60-second demonstration outline

1. Show the human request: “Ask Morgan's agent for the approved asset.”
2. Show only the selected source and requested action in Morgan's approval.
3. Show the agent being denied when attempting another source.
4. Approve the requested file and show its transfer and verified digest.
5. Send a question addressed to Morgan and show the answer attributed to a human.
6. State which parts use simulated agents; show real-host footage only after validation.

## Evidence to attach before broad outreach

- Exact app versions and an uncut recording of the real cross-machine flow.
- A conformance result covering denial, attribution, expiry and reconnects.
- A comparison with Agent Relay/Relay Connect using the same scenario.
- A reproducible setup, costs, security limits, and a specific contribution request.

Do not imply endorsement by OpenAI, Anthropic, Google, the A2A project, or a foundation. Do not mass-message maintainers or publish private correspondence. Publishing this repository does not authorize sending these drafts to anyone.
