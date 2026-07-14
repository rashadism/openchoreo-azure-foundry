# Azure AI Foundry resource types for OpenChoreo

Two building blocks that let a developer ask for an Azure AI Foundry **model** and
**agent** the same way they'd ask for a database — without touching Azure themselves.

The platform team installs these once. Developers then just say "give me a model"
and "give me an agent," and the platform provisions them in Azure and wires the
connection details into the app.

## What's here

| File | What it gives a developer |
|------|---------------------------|
| `resourcetypes/azure-foundry-model.yaml` | A model deployment (e.g. `gpt-5-mini`) inside an existing Foundry account |
| `resourcetypes/azure-foundry-prompt-agent.yaml` | A prompt agent (model + instructions, no code) in an existing project |
| `resourcetypes/azure-foundry-agent.yaml` | A hosted agent (your own container) in an existing project |
| `examples/chatbot.yaml` | A developer asking for a model + agent, then an app using them |

## How it works

```
Developer asks for a model/agent
        │
        ▼
OpenChoreo renders these templates into the cluster
        │
        ├─ model → Azure Service Operator creates it in Azure
        └─ agent → a short Job calls Foundry's API to create it
        │
        ▼
Connection details come back as outputs
        │
        ▼
The app's dependencies pick them up as environment variables
```

Credentials never sit in these files. Each environment points at its own Azure
account and its own identity, so dev and prod stay separate automatically.

## Two kinds of agent

Pick based on whether the developer ships their own code.

| | Prompt agent | Hosted agent |
|---|---|---|
| What it is | Model + instructions + tools | Your own container image |
| Needs code/image | No | Yes |
| Extra Azure setup | None | A container registry (ACR) the project can pull from |
| Use when | Most cases — a configured assistant | You need custom runtime logic |

**Start with the prompt agent.** It's just a model and instructions, so it needs no
registry and works with basic project access. Reach for the hosted agent only when
you have your own container to run.

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

## Before you use it

The platform team needs, per environment:

- An Azure AI Foundry **account and project already created** (these types add
  things *inside* them, they don't create the account).
- **Azure Service Operator v2.15.0+** installed on the cluster (for the model).
- A **ServiceAccount linked to an Azure identity** with the *Foundry Project
  Manager* role (for the agent's create call).

## Quick start

Install the two types:

```bash
kubectl apply -f resourcetypes/
```

Then a developer can use them — see `examples/chatbot.yaml`.

## Good to know about the agent

Azure has no "agent" resource you can declare — the only way to make one is to call
the project's API. So the agent type runs a Job that does exactly that, once.

That means:

- The agent is **created**, but not watched. Deleting it in Azure won't trigger a
  rebuild, and tearing down the resource won't delete the agent.
- In practice this is low-risk: Azure spins an idle agent's compute down after
  ~15 minutes, so a leftover agent costs almost nothing.

The model doesn't have this caveat — it's a real Azure resource, so it's fully
managed: created, kept in sync, and removed on teardown.

## Making the agent first-class (later)

To give the agent the same full lifecycle as the model, replace the Job with a
small controller that watches an agent object and calls the API on create, update,
and delete. A design and code skeleton for this lives in [`crossplane/`](./crossplane).

Until it's built, the Job is the simple, working choice.
