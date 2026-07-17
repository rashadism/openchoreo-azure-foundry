# Azure AI Foundry resource types for OpenChoreo

Two building blocks that let a developer ask for an Azure AI Foundry **model** and
**agent** the same way they'd ask for a database — without touching Azure themselves.

The platform team installs these once. Developers then just say "give me a model"
and "describe an agent," and the platform provisions them in Azure and wires the
connection details into the app.

## Recommended setup

Two types, each fully managed by its own operator:

- **Models → Azure Service Operator (ASO).** A model deployment is a control-plane
  (ARM) resource, so ASO reconciles it declaratively — create, self-heal, and
  delete-on-teardown all propagate to Azure. Use `azure-foundry-model.yaml`.
- **Agents → the Crossplane provider** (`crossplane/`). An agent is data-plane only
  (no ARM type), so ASO can't touch it; the provider manages it as a `FoundryAgent`
  CR with the same full lifecycle (create, heal on drift, delete via finalizer).
  Use `azure-foundry-agent.yaml`.

Developers pick only a model or describe an agent. All Azure account details — the
project endpoint and account ARM ID — come from a `foundry-account` ConfigMap the
platform team provisions **once per environment** (see below). No account details
are ever prompted to developers.

## What's here

| File | What it gives a developer |
|------|---------------------------|
| `resourcetypes/azure-foundry-model.yaml` | A real Foundry model deployment (e.g. `gpt-5-mini`), provisioned via ASO with full lifecycle |
| `resourcetypes/azure-foundry-agent.yaml` | A Foundry agent (model + instructions + MCP tools) as a `FoundryAgent` CR, managed by the Crossplane provider |
| `examples/chatbot.yaml` | A developer asking for a model + agent, then an app using them |

## How it works

```
Developer picks a model / describes an agent
        │
        ▼
OpenChoreo renders these templates into the cluster
        │
        ├─ model → Azure Service Operator creates/heals/deletes it in Azure
        └─ agent → the Crossplane provider reconciles a FoundryAgent CR
        │
        ▼
Both read the project endpoint from the foundry-account ConfigMap
        │
        ▼
Connection details come back as outputs; the app's dependencies pick them up as env vars
```

Credentials never sit in these files. Each environment points at its own Azure
account and its own identity, so dev and prod stay separate automatically.

## What the developer supplies

| | Model (`azure-foundry-model`) | Agent (`azure-foundry-agent`) |
|---|---|---|
| Managed by | Azure Service Operator (ASO) | Crossplane provider |
| Developer params | `modelName` (enum), `modelVersion`, `capacity`, `skuName`, `modelFormat` | `agentName`, `modelDeploymentName` (enum), `instructions`, `mcpServers[]` |
| Per-environment binding | `accountArmId` (owner for the ASO CR) | none |
| Account endpoint | read from `foundry-account` ConfigMap | read from `foundry-account` ConfigMap |

Neither type prompts the developer for the project endpoint or account ARM ID —
those come from the environment's `foundry-account` ConfigMap.

## Where's the API key?

There isn't one, on purpose.

- **Agents can't use keys.** Azure only allows identity-based (Entra ID) access to
  agents, so the app authenticates with its own Azure identity and just needs the
  endpoint.
- **Models default to the same.** The recommended setup is identity-based too, so
  the model hands back an endpoint and a deployment name — no secret.

That's why these types output endpoints, not keys.

If you specifically need a key for the model, this repo would also have to manage
the Foundry account (not just the deployment) and export its key into a secret.
That's an opt-in, not the default — open an issue if you want it.

## One-time PE setup: the `foundry-account` ConfigMap

The platform team wires each environment to its Azure Foundry account **once**, via
a `foundry-account` ConfigMap that carries the project endpoint and account ARM ID:

- Created in the **cell namespace** so the ResourceType templates can read it for
  outputs and render the ASO CR's owner (`accountArmId`).
- Created in the **provider's namespace** so the Crossplane provider can read the
  project endpoint when reconciling agents.

Because the account details live in this ConfigMap, developers never supply them —
they only pick a model or describe an agent.

## Before you use it

The platform team needs, per environment:

- An Azure AI Foundry **account and project already created** (these types add
  things *inside* them, they don't create the account).
- The **`foundry-account` ConfigMap** provisioned in the cell namespace and the
  provider's namespace (endpoint + account ARM ID).
- **Azure Service Operator v2.15.0+** installed on the cluster (for the model).
- The **Crossplane Foundry provider** running (see [`crossplane/`](./crossplane)),
  linked to an Azure identity with the *Foundry Project Manager* role (for agents).

## Quick start

Install the two types:

```bash
kubectl apply -f resourcetypes/
```

Then a developer can use them — see `examples/chatbot.yaml`.

## Lifecycle

Both types are fully managed — created, kept in sync on drift, and removed on
teardown:

- **Models** are real Azure ARM resources reconciled by ASO.
- **Agents** are `FoundryAgent` objects reconciled by the Crossplane provider in
  [`crossplane/`](./crossplane). It's implemented and verified end-to-end against a
  live Foundry project: applying a `FoundryAgent` creates the agent, deleting it in
  Azure makes the controller recreate it, and deleting the object removes the agent
  via a finalizer. See [`crossplane/DESIGN.md`](./crossplane/DESIGN.md) to run it.

A developer's `Resource` from `azure-foundry-agent.yaml` renders a `FoundryAgent`
CR, which the provider reconciles — so you get the dev-facing Resource abstraction
*and* full lifecycle, with no Job or token.
