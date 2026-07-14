# Azure AI Foundry resource types for OpenChoreo

Two building blocks that let a developer ask for an Azure AI Foundry **model** and
**agent** the same way they'd ask for a database — without touching Azure themselves.

The platform team installs these once. Developers then just say "give me a model"
and "give me an agent," and the platform provisions them in Azure and wires the
connection details into the app.

## What's here

| File | What it gives a developer |
|------|---------------------------|
| `resourcetypes/azure-foundry-model.yaml` | A model deployment (e.g. `gpt-4o-mini`) inside an existing Foundry account |
| `resourcetypes/azure-foundry-agent.yaml` | A hosted agent running inside an existing Foundry project |
| `examples/chatbot.yaml` | A developer asking for both, then an app using them |

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
and delete. The cleanest fit is a Crossplane provider. Two ways to build it:

- **Custom provider (Go):** the real answer — works today, full control, clean auth.
  It's a proper project to build and maintain.
- **Terraform-backed:** less code, but depends on Azure's `azapi` provider adding
  agent support, which isn't available yet.

Until then, the Job is the simple, working choice.
