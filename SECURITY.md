# Security

Airshower is experimental and has not received an independent security audit. The 0.3 P2P preview uses Iroh endpoint encryption and locally pinned contacts; review its [privacy and trust boundary](docs/p2p-desktop.md#privacy-and-trust) and [wire profile](docs/peer-to-peer.md). Native owner prompts protect the MCP API, not the entire OS account from other programs with the same access.

The separate HTTP reference relay remains trusted with plaintext and has no end-to-end encryption. Its [threat model](docs/threat-model.md) still applies; use TLS for remote deployment. Both transports block later access on revocation but cannot erase prior downloads or model context.

Never put tokens in URLs, Git, screenshots, public issues, or model prompts. Use separate credentials per agent and protect provisioning outputs as secrets. The demo uses throwaway credentials and fictional data.

## Reporting

Do not post exploitable security details or private data in a public issue. Use GitHub's private vulnerability reporting for this repository if available. If it is unavailable, open an issue containing only a request for a private reporting channel, without reproduction details. A response SLA and a bug bounty are not offered.

Only the current development line is maintained at present. Security fixes should include a regression test and explain which prior behavior was affected.
