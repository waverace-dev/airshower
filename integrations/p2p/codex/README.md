# Codex local plugin setup

Use the built `airshower-p2p-codex-0.4.0-macos-arm64.zip` from the release.
This source directory alone is not a complete plugin. The release includes
the adapter, native modules, owner/lock helpers and a pinned Node executable.

The setup agent can prepare an isolated local marketplace directory without
changing existing marketplaces:

1. Verify the downloaded release archive against its published SHA256SUMS.
2. Extract it into `plugins/airshower-p2p` under that dedicated directory,
   preserving executable permissions from the archive.
3. Copy this directory's `marketplace.json` template to
   `.agents/plugins/marketplace.json` under the new marketplace root. Its source
   path is relative to that root, not the `.agents/plugins` directory.
4. Open the plugin page using the host's supported local marketplace flow:
   `codex://plugins/airshower-p2p?marketplacePath=<URL-encoded absolute path to marketplace.json>`.
   The owner completes any installation/permission prompts. Preserve existing
   plugins, config, private profiles and rollback packages.
5. In an active chat call `ready` with the preferred display name, then exchange
   public cards and independently approve the contact on both sides.

An arbitrary GitHub ZIP URL is not a documented one-click Codex install target.
The phrase “Get me Airshower ready” is supported by the included onboarding
skill after the host loads the plugin. Public directory listing requires
provider review; this project has no such listing or approval. Actual desktop
loading still requires verification; protocol/package tests do not prove it.

Format and flow references:
- [Package your plugin](https://developers.openai.com/plugins/build/plugins)
- [Desktop-only plugins](https://developers.openai.com/plugins/deploy/desktop-only)
- [Desktop deep links](https://learn.chatgpt.com/docs/reference/commands#plugins)

See [P2P desktop behavior and limits](../../../docs/p2p-desktop.md).
