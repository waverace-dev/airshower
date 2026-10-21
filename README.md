<p align="center"><img src="docs/banner.svg" alt="airshower — people connected, agents coordinated" width="960"></p>

# Airshower

**Your agent. Their agent. A shared understanding.**

Airshower is an open proposal for people and their agents to exchange requests, context, and files across apps and computers—with explicit permission and clear attribution.

> “Ask Morgan for the final logo.”<br>
> “Ask their agent what changed in the project.”<br>
> “Have our agents coordinate a review, and involve us when needed.”

**Status: experimental v0.1 proposal and reference implementation.** Airshower is not an adopted standard. The local demo works with simulated agent clients; compatibility with live desktop conversations has not been established. See [what works](#what-works-today) before integrating it.

## Try the complete flow

Python 3.9+ and Git are sufficient. No model API key, account, package installation, or paid inference is needed for the demo.

```sh
git clone https://github.com/waverace-dev/airshower.git
cd airshower
python3 -m airshower demo
python3 -m unittest discover -s tests -v
```

The demo runs a temporary local relay, creates two fictional owners and simulated agents, exercises a human approval, transfers a fixture file, and checks the result. It uses no personal files or live AI providers. Real agents still use their own model subscriptions or API billing.

## The proposal

Agents can already send messages. People also need to know **whose agent is speaking, which information it may use, whether a human needs to answer, and what was actually shared**.

Airshower proposes a common profile for five interactions:

| Interaction | Meaning |
| --- | --- |
| Message | Deliver a message to a person's designated agent. |
| Ask a person | Wait for the person's own response, with human attribution. |
| Request context | Ask about explicitly shared sources, without opening every chat. |
| Request a file | Hand over a permitted file, retaining its name, type, and digest. |
| Delegate a task | Track a request through acceptance and a reported result. |

Permissions belong to the receiving person. A contact relationship allows incoming requests; an action/source grant or explicit human decision allows processing. An agent credential cannot create grants or impersonate an owner.

The reference implementation uses **curated context excerpts and explicitly uploaded files**. It does not crawl your chats or disk. Restricting the natural-language content an AI might infer or repeat needs additional application controls; source permissions alone cannot prove that a summary contains no sensitive information.

## What works today

| Capability | v0.1 status |
| --- | --- |
| Separate owner and agent identities | Implemented with distinct bearer credentials and server authorization. |
| Contacts, scoped grants, approval, expiry and revocation | Reference implementation, covered by tests. |
| Human answers and agent answers | Attributed by authenticated role, not client-supplied labels. |
| Context and file requests | Explicit sources, protected downloads, integrity checks. |
| Immediate events and reconnect replay | Durable SQLite event log and authenticated Server-Sent Events. |
| Existing assistant tools | MCP stdio adapter, explicitly pinned to revision `2025-11-25`; protocol tests, not host certification. |
| A2A interoperability | Optional A2A 1.0 HTTP+JSON facade with an enumerated subset; [scope and limitations](docs/a2a.md). |
| Existing desktop conversation wake-up | **Not implemented or verified.** MCP tool availability does not wake a chat. |
| Automatic chat access, local file discovery | **Not implemented.** Source connectors are future work. |
| Internet deployment | HTTPS deployment guidance; not yet independently audited or tested across two Macs. |
| Federation and end-to-end encryption | **Not implemented.** One shared relay is trusted with message contents. |

## How it fits

```mermaid
flowchart LR
    A[Person A] --> AP[Their permissions]
    AA[Their agents] <--> R[Airshower relay]
    R <--> BA[Other person's agents]
    B[Person B] --> BP[Their permissions]
    AP --> R
    BP --> R
    R --> H[Human questions and decisions]
```

**A2A** supplies an existing language for exchanging agent tasks. **MCP** supplies tools that an assistant can call. Airshower explores the ownership, permission, attribution, and handoff behavior around those interactions. The custom reference API is deliberately separated from the [A2A adapter](docs/a2a.md); an Airshower endpoint is not automatically an A2A endpoint.

## Read and build

- [Specification and permission model](docs/specification.md)
- [Protocol decision log](docs/decisions.md)
- [MCP setup and integration boundaries](docs/integrations.md)
- [A2A mapping and supported methods](docs/a2a.md)
- [Run your own relay](docs/self-hosting.md)
- [Threat model](docs/threat-model.md) and [security reporting](SECURITY.md)
- [Existing projects and what we still need to learn](docs/landscape.md)
- [Roadmap and adoption criteria](docs/roadmap.md)
- [Contributing](CONTRIBUTING.md) and [governance](GOVERNANCE.md)

## Help make the proposal useful

The most useful contribution is a reproducible interoperability result: two real agents owned by different people, a scoped permission, and a complete request/reply or file handoff. Report the app versions, supported delivery behavior, and failures. Do not submit private chats or credentials.

We welcome implementers from every model provider and independent project. No provider is excluded, no membership is required, and the protocol is intended to be implementable without using our relay. The Apache-2.0 license covers this repository. The name and logo are not a certification mark.

**No lab endorsement or adoption is claimed.** Adoption should follow working implementations, transparent tests, and shared governance—not a badge in this README.
