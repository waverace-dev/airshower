# Get Airshower ready

Airshower adds peer communication inside your existing agent chat. The **0.4 P2P
preview** targets Apple silicon Macs, macOS 13.5+. No Airshower account server,
enrollment invitation, approval password, or end-user Terminal is needed.

Tell your setup agent:

> Get me Airshower ready from https://github.com/waverace-dev/airshower. Follow INSTALL.md and tell me what is actually verified.

## Desktop packages

Get built packages from the [P2P preview release](https://github.com/waverace-dev/airshower/releases/tag/v0.4.0-preview.1).

- **Claude Desktop:** install `airshower-p2p-0.4.0-macos-arm64.mcpb` through its
  extension interface. Enter only your display name. Claude supplies Node.
- **Codex desktop:** have your setup agent load
  `airshower-p2p-codex-0.4.0-macos-arm64.zip` through the supported local plugin
  mechanism. It includes Node and an onboarding skill. Do not substitute the
  source-only Git directory or ask the person to type shell commands. Report
  any missing host-loading prerequisite honestly.

The setup agent can follow the concrete [Codex local marketplace recipe](integrations/p2p/codex/README.md).

These are previews, not vendor-directory installations. Native helpers are
ad-hoc signed, not Apple notarized. Installation in the actual apps and a real
two-Mac exchange still need verification. Do not bypass host security checks.

## Connect someone

1. Say **“Get me Airshower ready.”**
2. Ask for your **public connection card**, and exchange cards with the intended
   person through an existing trusted conversation.
3. Each person tells their agent to connect the other's card, then approves the
   native window. Both directions are required. Connection grants no source
   access or task permission.
4. Keep both chats active for a harmless request/reply test. The recipient
   checks their inbox and approves or answers in the native window. Confirm
   that the result returns with correct attribution.

Only deliberately published context and owner-selected files can be requested.
Each owner can require approval every time or approve exact kinds/sources. A
question for the human always needs their actual answer.

Read [desktop setup, privacy and limits](docs/p2p-desktop.md) and
[verification evidence](docs/p2p-verification.md).

**P2P receipt does not wake an idle Claude Desktop or local Codex chat.** The
bridge receives while its process runs; active-chat tools check or wait for
work. Both devices must overlap online. There is no offline mailbox. An
[optional background helper](docs/background-agent.md) can answer permitted
requests while the extension is running, using an already installed and signed-in
subscription runtime. Say “Enable Airshower’s background helper using Codex”
or “…using Claude,” then approve the native window. Each app has its own
identity; one active MCP process per app profile is supported in this preview.

## Earlier HTTP relay preview

The [0.2 release](https://github.com/waverace-dev/airshower/releases/tag/v0.2.0-preview.2)
and [its setup instructions](docs/desktop-setup.md) remain for comparison and
rollback. It uses a server trusted with plaintext and is not P2P. Do not
silently migrate or erase existing profiles. Avoid simultaneously enabling old
and new tools with confusing names. Building a release changes no user app
settings or startup entries.
