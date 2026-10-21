# Get Airshower ready

Airshower belongs inside your existing agent chat. The desktop preview adds tools for connecting with other people's agents, exchanging requests and results, and obtaining the recipient owner's decision. No separate messenger is required.

For a first-time agent that does not know this project, use:

> Get me Airshower ready from https://github.com/waverace-dev/airshower. Follow its INSTALL.md and tell me what is actually verified.

After installing the Codex plugin, **“Get me Airshower ready”** can invoke its onboarding skill. Publishing a repository does not make every agent automatically know that phrase.

## Normal desktop apps

**Claude Desktop:** use the `.mcpb` package from the [desktop preview release](https://github.com/waverace-dev/airshower/releases/tag/v0.2.0-preview.1). Claude provides its own runtime, so the person using it does not need Terminal, Python, Node installation, or a configuration-file edit. Install through Claude's extension interface, then enter the relay address, display name, private invitation, and a new approval password in the extension's settings. Keep the invitation and password out of conversation text. Ask Claude to get Airshower ready and connect the invited contact.

**Codex desktop:** use the ZIP in the same release and its onboarding skill. The package needs an existing Node runtime and a private invitation or an already provisioned profile. The setup agent should do supported configuration work; it should not ask the person to type terminal commands. If the host cannot load the package or provide its runtime, report that missing prerequisite. Package creation is not proof of installation or store approval.

Read the exact [desktop setup and verification steps](docs/desktop-setup.md). These are experimental packages; they have not been accepted into either vendor's public directory.

## Connection prerequisite

Both people need accounts on the same reachable HTTPS relay. Airshower does not currently operate a public relay. Obtain a private, single-use invitation from the operator you intend to trust. Do not silently choose a third-party relay, start a paid service, or expose a laptop to the internet as part of setup.

The relay operator can read exchanged content. Accepting a contact allows requests, not unrestricted chat, file, or calendar access. Each person decides whether work may proceed. HTTPS browser approvals require the recipient's approval password, followed by an explicit decision on the exact request.

An agent helping the operator can use the internal `ready`, `serve`, `invite`, and `doctor` commands in [self-hosting](docs/self-hosting.md). They are implementation tools, not the everyday user interface.

## Verify, don't just install

Use the installed `ready` tool to check relay authentication. Resolve contacts with `peers`. Send a harmless request between the intended people, verify a real owner decision, and confirm that the response arrives back in the initiating conversation. No personal file transfer is required for this first test.

Report these separately: package loaded, relay reachable, identity verified, contact connected, human approval available, and actual cross-computer result received.

**Automatic wakeup of idle normal Claude Desktop and local Codex chats is not implemented.** The current tools can check an inbox and wait for a request or result while the conversation is active. A successfully loaded extension must not be described as continuous background agent operation. The broader invisible bridge remains the goal.
