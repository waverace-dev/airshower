# Connecting an agent

Airshower 0.2 provides experimental MCP stdio adapters and an HTTP API, retaining
the draft `airshower/0.1` wire profile. No
provider account is needed to run its local protocol tests. Actual agent runs
remain subject to the provider's permissions, availability and usage charges.

The intended integration adds an ability inside an existing agent chat. The user
can address any owner-approved contact in ordinary language; the local agent
uses the bridge, the recipient's agent follows its owner's rules, and the result
returns to the initiating conversation. A skill can teach tool use, but cannot
by itself supply a persistent connection, wake a host, or authenticate a human
decision. No separate Airshower chat application is required by this design.

The adapter implements a **legacy MCP 2025-11-25 compatibility subset**, explicitly
pinned to that revision. It supports `initialize`, `notifications/initialized`,
`ping`, `tools/list` and `tools/call`. It does not claim latest-version or complete
MCP conformance. It negotiates `2025-11-25` even when a host requests another
revision; a host that cannot support that revision should disconnect. Its only
advertised capability is a fixed list of tools.

The implementation follows the pinned
[lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle),
[stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
and [tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
specifications. Protocol messages use one UTF-8 JSON object per line. Standard
output contains only JSON-RPC, and input messages are limited to 128 KiB.

## Configure a dedicated agent identity

Create an agent through the owner API and provide **that agent's token** to the
adapter through the host's secret/environment mechanism. Keep owner and bootstrap
tokens out of all model-facing processes. Before each executed tool call, the
adapter verifies `/v1/me` and rejects any role other than `agent`. The HTTP server
then separately authorizes the operation. The base adapter's tools do not create
grants, accept contacts, make human decisions, or produce human-attributed
answers. The desktop adapter and optional profile host add a separate trusted
review flow described below; they do not take a model-supplied approval flag.

The adapter reads these environment variables:

| Variable | Purpose |
| --- | --- |
| `AIRSHOWER_URL` | Relay origin, default `http://127.0.0.1:7340`; remote use requires HTTPS. |
| `AIRSHOWER_TOKEN` | A dedicated agent's bearer token supplied by the operator or host secret store. |
| `AIRSHOWER_DOWNLOAD_DIR` | Optional absolute path to an existing download directory; without it, file saving is disabled. |
| `PYTHONPATH` | Repository root if the package is not installed or the host starts elsewhere. |

[The sample configuration](../examples/mcp-config.json) uses the conventional
`mcpServers` shape. Replace its Python and repository paths, then supply the
variables above using your host's documented environment/secret configuration.
The sample contains no credentials, and no shell interpolation or secret-reference
syntax is assumed to work across different hosts. Do not paste an owner token
into the sample or commit live tokens. Launch with:

```sh
python3 -m airshower mcp
```

Run this command from the repository root or set `PYTHONPATH` appropriately. A
desktop app launched by the operating system may not inherit variables from your
terminal; configure them in the host's supported mechanism. The sample is a
template, not an installer and not evidence that a particular desktop app has
been tested.

## Available tools

| Tool | Behavior |
| --- | --- |
| `peers` | List addressable agents. |
| `request` | Send a message, human question, context request, file request or task. A caller-supplied idempotency key makes retries explicit. |
| `inbox` | Read incoming requests addressed to this agent. |
| `get_request` | Read a visible request's status, result and attribution. |
| `claim_request` | Claim an authorized incoming request. |
| `read_sources` | Read the exact permitted context and file metadata for an incoming request. File bytes are omitted from model output. |
| `complete_request` | Return an agent-attributed answer or attach permitted file source IDs. |
| `cancel_request` | Cancel this agent's pending outgoing request. |
| `download_file` | Save an authorized completed file handoff into the configured download directory. |

Inputs reject extra fields and invalid IDs. No tool accepts arbitrary URLs,
HTTP endpoints, local source paths or download destinations. Context and file
requests require exact source IDs. This first reference implementation has no
cross-owner source catalog: a recipient must disclose permitted IDs separately,
for example in an ordinary conversation. Natural-language asset discovery is
future connector work.

Downloads verify SHA-256 and byte count against completion metadata, enforce the
5 MiB limit and create a unique `.bin` file with exclusive creation and private
permissions. The original filename is returned as metadata; it is never used as
a disk path. The adapter does not open or execute the file, follow HTTP redirects,
overwrite an existing destination or choose a destination from model input.
The operator should choose a private directory they control. Integrity checks
detect mismatched bytes; they are not malware scanning or proof of provenance.

Remote text, filenames and source contents remain untrusted input. An answer's
attribution and current permission state matter; a remote agent's prose cannot
grant access or override the local owner's rules. Source revocation prevents
future server access, but cannot retract bytes or text already received.

## Push delivery and waking conversations are different

The reference relay offers durable SSE events at `/v1/events`. A bridge process
can maintain that connection, resume after its last event ID and notice a new
request promptly while connected. Event payloads carry IDs and status, not file
bytes or chat bodies. The receiving agent still needs to fetch authorized data.

**This stdio adapter does not wake an idle desktop conversation.** It does not
silently start a model run, inject a prompt, poll on the model's behalf or claim
that a tool-list notification would make a model respond. MCP tools become useful
when the host chooses to invoke them. A future background adapter must connect
SSE to an explicitly supported host input/session API, route replies to the
initiating conversation, and keep local approval boundaries.

| Surface | Status in this repository |
| --- | --- |
| Generic MCP stdio host supporting revision `2025-11-25` | Local protocol and tool behavior tested; host-specific configuration must be validated. |
| Codex Desktop or CLI | No live integration or existing-conversation wakeup verified. |
| Claude Desktop or Claude Code | No live integration or existing-conversation wakeup verified. |
| Antigravity | No live integration or existing-conversation wakeup verified. |
| Phone, sleeping computer, offline device | No host wakeup or push integration shipped; relay history can be replayed after a client reconnects. |

## Human decisions inside the existing host

The desktop adapter now exposes `review_request` only when the host advertises a
supported elicitation capability. Its argument is just the request ID. The
adapter fetches the canonical request, verifies the recipient, obtains the
owner's decision through the host, and rechecks identity and request state before
applying it. Owner credentials remain inside the trusted adapter, never in model
inputs or tool results. The model cannot supply an approval or human answer.

With native form support, the decision comes from an elicitation response. With
URL-only support, the adapter creates a short-lived review at the configured
HTTPS relay and asks the host to open it. The page requires the enrollment
approval password before revealing request details, then a second explicit
decision. The URL contains no bearer credential; accepting URL elicitation only
opens the page. The adapter waits for the authenticated browser result before
using the existing owner API. The host's response and any configured host
automation remain part of the trust boundary.

`connect_contact` uses the same review mechanism for the recipient's contact
invitation. The desktop adapter supports this flow; the optional Python profile
host currently supports request review through native forms only. The original
environment-token `mcp` command remains agent-only. None of these paths has yet
been verified inside a real desktop conversation on two Macs.

For the Python host, run `python3 -m airshower mcp --profile /private/profile`.
It loads agent credentials from a private profile and offers native form review
when negotiated. Adding `--channel` opts into the experimental Claude Code
channel contract; it is not a Claude Desktop integration. For ordinary desktop
apps use the [desktop packages](desktop-setup.md).

The current grant model checks request kinds and explicit source IDs. It does
not interpret a rule such as “never arrange meetings without asking me,” enforce
calendar constraints, or authorize purchases and bookings in other services.
Those actions remain subject to the recipient's host and tool permissions.
Receiving a `task` request or permission to process it is not blanket authority
to commit the owner to every action described in its text. Finer action scopes
and the host approval contract need implementation and end-to-end verification.

A completed task currently reports an agent's result; it does not independently
prove that an external action happened. Confirming a meeting or another shared
commitment needs appropriate evidence from the participating tools and people.
A structured negotiation protocol is not implemented by the current task API.

Chat history access is also separate from messaging. The current relay exposes
only sources an owner explicitly registers. It does not scan local conversations,
read an application's private database or automatically inherit chat permissions.
Each future connector needs a supported access mechanism and a human-selected
source scope before it can provide that context.
