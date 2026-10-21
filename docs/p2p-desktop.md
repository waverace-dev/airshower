# Airshower P2P desktop preview

Version 0.3.0 adds encrypted peer communication to your existing agent chat.
There is no Airshower account server, hosted approval page, or new messenger.
This is a testable preview, not an adopted standard or certified integration.

## Get ready without Terminal

The release targets **Apple silicon Macs, macOS 13.5+**. Intel, Windows and
Omarchy desktop packages are not provided by this release.

1. Get the matching package from the
   [P2P preview release](https://github.com/waverace-dev/airshower/releases/tag/v0.3.0-preview.1).
   In Claude Desktop, use the desktop extension installer to open
   `airshower-p2p-0.3.0-macos-arm64.mcpb`, then enter your display name. Claude
   supplies Node. In Codex, have your setup agent load
   `airshower-p2p-codex-0.3.0-macos-arm64.zip` through the supported local plugin
   mechanism. The ZIP includes Node; the source-only Git directory is not the
   built plugin. No end-user Terminal or runtime installation is needed.
2. Say **“Get me Airshower ready.”** Ask for your **public connection card**.
   It contains a public identity and network addresses, never a private key.
3. Exchange cards through an existing trusted conversation with the intended
   person. Each person tells their agent to connect the other's card and
   approves a native Airshower window. Names are self-asserted; verify the
   source of the card and the displayed key. Both directions are required.
4. Keep both chats active for the first harmless request. On the receiving side,
   say **“Check my Airshower requests.”** Review the exact request in the native
   window. A human question is answered there; other work becomes available to
   the agent after approval or a previously approved rule.
5. Confirm that the attributed result returns to the initiating conversation.
   Queued or delivered is not a confirmed result.

Native helpers are ad-hoc signed, **not Developer ID signed or notarized**.
The actual vendor installer, Gatekeeper behavior, and a two-person desktop
roundtrip still need verification. Do not disable Gatekeeper or certificate
validation to load a preview. Report the specific installation failure. Neither
vendor's public directory has accepted this package.

The older 0.2 relay extension/profile remains separate and is not migrated or
deleted. If previously enabled, disable its connection before testing P2P to
avoid confusing duplicate tool names; preserve its package/profile for rollback.

## Connection and permissions

Each app profile owns an Ed25519 endpoint identity and local request journal.
Iroh establishes authenticated, encrypted QUIC connections. Public discovery
finds changing addresses; Iroh tries direct connectivity and can forward
encrypted packets through a public relay when needed. The relay does not
authorize requests, run an AI, or store an offline inbox.

The receiving bridge checks the connection's cryptographic identity against
locally approved contacts before accepting application messages. Payload fields
cannot replace the authenticated sender. Pairing starts with no automatic task
or source grants. Incoming requests default to owner review. Standing rules
cover exact kinds and source IDs; `ask_human` always needs a human answer.
Airshower permission does not authorize calendar writes, spending, messages to
third parties, or other host tools; those retain their own applicable rules.

Requests arrive while the MCP process is running. Results are pushed over the
live connection, with bounded retry and polling recovery. A private sender
outbox survives restarts and replays idempotently. Both bridges must eventually
overlap online before delivery can succeed. Cancellation remains pending until
the peer acknowledges it; work may already have completed. Requests expire
after 24 hours. An OS file lock prevents concurrent profile writers and releases
after a crash.

**Network receipt does not wake an idle normal Claude Desktop or local Codex
chat.** Check/wait tools work during an active conversation. No model, login
item, LaunchAgent, or background service is installed. The preview supports
one active MCP process per app profile; other simultaneous processes fail
closed. Codex and Claude have separate identities. A shared local daemon is
future work.

## Context and files

`publish_context` shows the exact text to the owner. `publish_file` opens a
native picker; the model cannot submit a path. The bridge takes a private copy
of the chosen regular file. Each peer's catalog contains only sources expressly
published to that peer. Requests still need approval or a matching standing rule.

A completed handoff contains source IDs, filenames, lengths and SHA-256 digests.
Download rechecks live contact/source permission, verifies bytes, and creates a
new private `received-<uuid>.bin` file. It never overwrites the original, follows
an input-file symlink, opens or executes a download. The returned original name
can be used for an explicitly authorized later copy/rename. Revocation blocks
future reads/downloads; earlier copies and model context cannot be retracted.

Preview bounds: 50 contacts, 256 retained requests, 30 sources, 5 MiB per file,
10 MiB of published file bytes and 20 MiB of retained downloads per profile,
16 KiB request/result text, and
64 KiB context excerpts (text limits count JSON encoding). At capacity the
bridge fails instead of silently deleting history. Archival and large-file
streaming remain future work.

## Privacy and trust

Keys, permissions, published snapshots and history stay in owner-only files in
`~/Library/Application Support/Airshower/p2p/<app>/`. They are not additionally
encrypted at rest. There is no automatic access to other chats or arbitrary
files. Content returned to an agent is visible to its host/model provider,
subject to that host's policies.

Public Iroh infrastructure cannot decrypt peer payloads, but sees network
metadata: public endpoint identity, IP addresses, timing and volume. Discovery
publishes identity/routing information. Direct peers learn each other's IPs;
public cards can include IP addresses. Share cards with intended contacts rather
than posting them publicly. This is not anonymity. The
[public relays](https://docs.iroh.computer/iroh-services/relays/public) are free
for development/hobby use, rate-limited, without an SLA, and officially support
only current stable Iroh. Free production hosting is not promised.

Native prompts enforce the MCP tool boundary, **not an OS sandbox against
programs running as the same user**. A host agent with unrestricted shell/file
access is outside that boundary. A `human` reply means the authenticated remote
bridge reports collecting owner input; it is not independent cryptographic
proof of a person's identity. Trust the contact and their bridge implementation.

The bridge needs no model API key or paid inference. Existing agent subscription
limits still apply. Model credentials are not relayed or shared.

## Developer checks

Build inputs are pinned in `dependencies.lock.json` and `node-runtime.lock.json`
under `integrations/p2p`. Fetchers verify archive hashes, extract bounded regular
files, run no package scripts, and write only into the project.

```sh
python3 scripts/fetch_p2p_dependencies.py
python3 scripts/build_p2p_native.py
node --test integrations/p2p/tests/*.test.mjs
# Apple silicon release build:
python3 scripts/fetch_p2p_runtime.py
python3 scripts/build_p2p.py
```

Fetchers preserve existing destinations. Keep old inputs for rollback. Normal
tests use local endpoints with discovery/relays disabled. Opt-in internet smoke
tests use only temporary fictional identities and harmless fixture text, and
report the actual path. Do not upload private profiles or real owner approvals.

See the [wire profile](peer-to-peer.md) and [verification evidence](p2p-verification.md).
