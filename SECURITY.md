# Security

Airshower 0.1 is a reference implementation, not an audited public-facing service. Review [the threat model](docs/threat-model.md) before deployment. Bind locally and use a trusted HTTPS reverse proxy or authenticated tunnel for remote use.

Owner credentials can approve sharing. Agent credentials cannot. The shared relay operator can access stored contents; there is no end-to-end encryption in this version. A grant revocation can block later access but cannot erase material already downloaded or placed in a model context.

Never put tokens in URLs, Git, screenshots, public issues, or model prompts. Use separate credentials per agent and protect provisioning outputs as secrets. The demo uses throwaway credentials and fictional data.

## Reporting

Do not post exploitable security details or private data in a public issue. Use GitHub's private vulnerability reporting for this repository if available. If it is unavailable, open an issue containing only a request for a private reporting channel, without reproduction details. A response SLA and a bug bounty are not offered.

Only the current development line is maintained at present. Security fixes should include a regression test and explain which prior behavior was affected.
