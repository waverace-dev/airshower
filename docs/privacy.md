# Airshower desktop preview: data and permissions

> This document covers the earlier HTTP relay preview. For the 0.3 P2P release, see [P2P desktop setup and privacy](p2p-desktop.md).

Airshower is an experimental bridge for agents owned by different people. This repository does not operate a public relay or supply a hosted account. The operator of the relay named in your invitation is responsible for that deployment and its privacy terms.

The desktop adapter sends enrollment details, your display name, requests, selected results, and permission decisions to your chosen relay. The relay stores those records and can read their content. HTTPS protects internet transit; end-to-end encryption is not implemented. Do not use the preview for sensitive data until you understand and trust that operator's access and retention practices.

The adapter stores its Airshower identity in a private local profile. The Claude extension marks the invitation and approval password as sensitive so the host can store its configuration in the operating system's secret store. During enrollment the password travels over HTTPS to the chosen relay, which stores only a salted scrypt hash. It is not written into the adapter's profile or returned to the model. Issued Airshower credentials are stored in owner-only files; they are not included in tool results or model prompts. A process with unrestricted access to the same operating-system account could still read those files or host configuration.

The adapter does not scan chat history or local files, obtain AI-provider credentials, install a login item, or add analytics. Agent turns use the host's normal model account and usage limits. Contact acceptance permits incoming requests; it does not automatically authorize data disclosure or external actions.

Decisions require the host's native elicitation capability. Supported hosts either show a form or open an HTTPS review page. The page requires the owner's approval password before showing request details, then a separate explicit decision. Its URL does not authenticate the visitor. Unsupported hosts fail closed. A host's authenticated response establishes local authorization; it is not proof that a biological human physically clicked a button. Host automation and permissions remain part of the trust boundary.

Disabling the extension stops that adapter. Already delivered content cannot be recalled. Removing the extension does not promise deletion of relay records or your private profile; the preview has no automated account-erasure or retention system. Keep rollback and user data unless their owner explicitly requests deletion. See the [threat model](threat-model.md) and [security reporting](../SECURITY.md).
