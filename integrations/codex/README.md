# Airshower for Codex desktop

This directory is the **source of a portable Codex plugin**, not a complete
installed plugin. The release builder must copy `plugin.json`, `mcp.json`, and
`skills/`, then bundle `integrations/desktop/server.mjs` as `server/server.mjs`
alongside the license. The server has no npm dependencies. An available Node.js
runtime is required; this package does not install one.

The plugin adds Airshower to an existing local chat. Its onboarding skill starts
when the user chooses setup, including in the current conversation when the
plugin was installed there. Setup still needs a valid relay profile or the
adapter's supported invitation flow. It does not create a globally hosted relay.

## Local testing and distribution

`marketplace.json` is a template for the marketplace root's
`.agents/plugins/marketplace.json`. Place the built plugin at
`plugins/airshower` beneath that root. `source.path` resolves from the marketplace
root, not the `.agents/plugins` directory. Do not copy the template over an
existing marketplace; merge the intended entry while preserving other plugins.

The desktop app's Plugins Directory can install from this local marketplace.
For an agent-assisted setup, a generated link can open the plugin page:

```text
codex://plugins/airshower?marketplacePath=<URL-encoded absolute path to marketplace.json>
```

The normal install deep link requires an already-known marketplace. An arbitrary
GitHub ZIP URL is not a documented one-click install target. Public directory
submission of a local MCP server additionally needs OpenAI allowlisting and
review. This repository is not listed or approved by OpenAI.

While a chat is active, `wait_for_request` can wait for durable incoming events,
and native MCP elicitation can collect owner decisions when the host supports
it. Installing this package does **not** wake an idle Codex Desktop chat. Work
Cloud event subscriptions are a different supported surface and are not enabled
here.

Official format and behavior references, checked 2026-10-21:

- [Package your plugin](https://developers.openai.com/plugins/build/plugins)
- [Desktop-only plugins](https://developers.openai.com/plugins/deploy/desktop-only)
- [Desktop deep links](https://learn.chatgpt.com/docs/reference/commands#plugins)
- [MCP events and supported surfaces](https://developers.openai.com/plugins/build/mcp-events)
