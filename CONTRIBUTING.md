# Contributing

Start with a concrete interaction someone cannot complete today. We prefer small, demonstrated improvements over broad compatibility claims.

## Development

Run directly from the checkout with Python 3.9+:

```sh
python3 -m unittest discover -s tests -v
python3 -m airshower demo --json
```

The runtime and tests use the standard library. No provider credentials are needed. Keep new runtime dependencies exceptional and justified.

Changes affecting authorization, human attribution, source disclosure, event delivery, or files need negative tests. Include compatibility notes when changing wire behavior. Never use real conversations, credentials, or personal files as fixtures.

## Particularly useful contributions

- Verified adapters for specific versions of Codex, Claude Code/Desktop, Antigravity, and other hosts.
- Independent implementations of the profile, including constrained environments.
- Tests for offline reconnects, duplicate sends, permission expiry, and revocation.
- Human-friendly pairing and approval flows with accessible interfaces.
- A2A interoperability tests using independently maintained clients.

In your PR, state what changed, which behavior was tested, and what remains unverified. Run the standard tests before submitting. Avoid turning optional or unverified behavior into a normative requirement.

All contributions are under Apache-2.0. Preserve third-party notices and document copied material. Do not introduce code with licenses that exclude particular providers or users. Respectful, technical disagreement is welcome; harassment and private-data disclosure are not.
