# Optional background helper

Version 0.4 adds an opt-in helper that starts when an incoming P2P request
arrives, while the desktop extension process is alive. The owner still sets up
and controls Airshower from their usual agent chat. This is a separate, short
agent run; it does not wake or continue the original desktop conversation.

## Turn it on from your chat

After pairing, say **“Enable Airshower’s background helper using Codex”** or
**“…using Claude.”** The setup agent calls `enable_background_agent`. A native
window shows the runtime, usage consequences and scope for the owner to approve.
Declining leaves the helper disabled. Existing profiles start with it disabled.

The selected official **Codex or Claude Code runtime must already be installed
and signed in with the owner's subscription**. The ordinary Claude Desktop app
and Claude Code have different runtime/sign-in prerequisites. Airshower detects
known installation locations, checks required options, and reports missing
prerequisites; it does not download programs or extract login tokens. The setup
agent can arrange vendor setup with the owner's authorization, without asking
the person to type terminal commands. This preview does not yet include a
complete graphical installer/sign-in flow for a missing vendor runtime.

The runtime uses the person's existing subscription allowance. It does not
silently fall back to an inherited API key. This is not unlimited/free model
usage: provider limits and the person's billing settings still apply. A usage
limit, expired login, or changed executable stops the job with a visible status.
After a vendor update, re-enable the helper to review its new version.

Useful requests in the existing chat:

- **“Check Airshower background status.”** See recent jobs, drafts and errors.
- **“Disable Airshower’s background helper.”** Stop new work and cancel the
  active run. Already delivered replies cannot be retracted.
- **“Retry that Airshower request.”** One native owner confirmation retries a
  failed, interrupted or dismissed request, subject to the same limits.

## What happens on receipt

1. The bridge authenticates the pinned contact and saves the request.
2. A local event wakes the helper coordinator. No model polls an empty inbox.
3. Requests without standing permission open the existing native owner review.
   A human question opens a reply field and sends only the actual typed answer.
4. After authorization, the helper receives just this request and its exact
   deliberately published context. It has no access to the original chat history.
5. A validated answer is sent back over P2P with agent attribution. File requests
   hand off only their approved file IDs, without asking a model to open files.

| Request | Background behavior |
| --- | --- |
| Message or context question | Answer from provided information; ask for attention if information or authority is missing. |
| Ask the human | Show native review; return the exact human answer without calling a model. |
| Approved file | Complete the scoped handoff; the sender can download the verified bytes. |
| Task involving another app or a commitment | Keep a local draft and show an attention notice. The main agent/owner must do the actual work and complete it. |

Requests needing the owner are not marked completed or sent as successful
actions. For example, a helper cannot promise that a meeting was booked or that
a payment was made. Calendar, banking and other action integrations remain
separate work. The owner can later grant those capabilities through explicit
connectors; this release does not grant them.

## Bounds and recovery

One request runs at a time, at most **10 requests per hour** per app profile.
The counter survives a process restart and includes requests that need owner
review. Model execution is bounded to two minutes and 2 MiB of process output;
context is capped at 256 KiB and result text at 16 KiB. Human prompts retain
their five-minute timeout. Dismissed prompts do not keep appearing.

Replay does not rerun a completed job. A crash during review or inference marks
the local job interrupted on restart; ambiguous execution is never retried
automatically. Explicit retries still require owner confirmation. Contact or
source revocation, cancellation and disabling the helper cancel an active run;
permissions are checked again before any answer is committed. Information
already sent to a provider or contact cannot be recalled.

Closing the extension stops its helper. No login item, launch service or
always-on daemon is installed. A sleeping Mac cannot receive live P2P requests;
the sender's outbox waits for overlapping connectivity. The original sender's
desktop chat also remains idle until it checks the returned result. Shared
multi-app identity and concurrent extension processes remain future work.

## Runtime and privacy boundary

Claude Code runs with customizations disabled, no built-in or MCP tools,
non-interactive permission denial, structured output and no saved conversation.
Codex runs ephemerally with user configuration and project instructions excluded,
read-only sandboxing, and shell, apps, plugins, hooks, memory, browsing, image,
and multi-agent features disabled. Its Code Mode host is disabled too. The
bridge rejects outputs containing unexpected tool execution. It passes request
data through stdin, without shell interpolation or provider credentials in
arguments. Native helpers remain ad-hoc signed, not notarized.

These are vendor runtime controls, not a separate OS security boundary against
other processes running as the same user. Managed vendor policy may still
apply; runtimes keep their own authentication, diagnostics and usage records.
The model provider receives the approved request/context. Airshower stores the
local job state and any attention draft in the private profile. It does not
retain raw runtime diagnostics, copy credentials, or automatically read other
chats. The runtime's executable digest is pinned on owner approval.

## Evidence, 2026-10-22

- A real Codex CLI `0.163.0-alpha.11` subscription run answered a fictional
  incoming request over real encrypted local P2P, starting from an idle helper,
  without a recipient inbox tool call. The reply returned in approximately
  5.5 seconds with agent attribution.
- Claude Code's subscription login and launch path were verified. Its live
  model request returned HTTP 429 `usage_limit_reached`. A successful Claude
  model reply is **not verified**; the job remained recoverable and sent nothing.
- Automated tests cover consent, human answers, duplicate delivery, cancellation,
  revocation during inference, scoped context, task deferral, restart ambiguity,
  serialization, queue wakeup races, durable quotas, timeout/output limits and
  vendor result parsing. All 263 local tests passed: 193 reference tests,
  39 HTTP desktop adapter tests, and 31 P2P/background tests.
- Extracted Claude and Codex release archives passed a two-process MCP test with
  real encrypted local P2P and a fictional model adapter. The idle recipient
  processed and returned the new request without a recipient inbox call.
- Neither this evidence nor an extracted-package fixture proves installation
  in the normal desktop apps, two physical Macs, or original-chat wakeup.

Runtime references: [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[Codex configuration](https://learn.chatgpt.com/docs/config-file/config-basic),
[Claude CLI options](https://code.claude.com/docs/en/cli-reference), and
[Claude Code distribution conditions](https://code.claude.com/docs/en/legal-and-compliance).
