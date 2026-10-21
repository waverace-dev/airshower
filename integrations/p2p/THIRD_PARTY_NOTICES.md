# Third-party build inputs

Airshower source is Apache-2.0. P2P archives also contain:

- `@number0/iroh` 1.1.0 (MIT OR Apache-2.0, redistributed here under MIT), and
  `@number0/iroh-darwin-arm64` 1.1.0 (MIT), from the official npm registry.
  `licenses/IROH-MIT.txt` comes from the upstream `v1.1.0` tag at
  <https://github.com/n0-computer/iroh-ffi>. Archive integrity is pinned in
  `dependencies.lock.json`. The native binary is the upstream prebuilt artifact;
  Airshower has not independently audited it.
- The Codex ZIP additionally contains official Node.js v24.22.1 for macOS arm64.
  Its complete bundled dependency notices are in `licenses/NODE-LICENSE.txt`.
  `node-runtime.lock.json` pins the archive SHA-256 from the matching official
  release SHASUMS256 file. Claude uses its host-provided Node runtime.

Fetchers do not run package scripts or install global runtimes. Iroh's JavaScript
entry is loaded by its explicit root path because the published 1.1.0 `main`
field points at a nonexistent subdirectory. Vendor files are unchanged.

Public discovery and relays are operated by n0, not Airshower. Their policies are
separate from the source licenses:
<https://docs.iroh.computer/iroh-services/relays/public>.
