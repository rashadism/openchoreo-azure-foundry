# provider-foundry (design)

A small Crossplane provider that makes a Foundry agent a real, managed object —
so it gets created, kept in sync, and deleted like any other resource, instead of
the one-shot Job.

## Why

Azure has no resource type for an agent; the only way to manage one is its project
REST API. Crossplane's job is exactly this: watch a custom object and call an
external API to make reality match it. That's a better fit than a Job, which only
ever runs once.

## The object

A `FoundryAgent` holds what the agent should be:

```yaml
apiVersion: foundry.openchoreo.dev/v1alpha1
kind: FoundryAgent
metadata:
  name: support-bot
spec:
  forProvider:
    projectEndpoint: https://acct.services.ai.azure.com/api/projects/chat
    agentName: support-bot
    image: myacr.azurecr.io/support-bot:v1
    modelDeploymentName: gpt-4o-mini
```

## How it maps to the API

The provider implements four methods. Each is one call to the project:

| Method | When it runs | Foundry call |
|--------|--------------|--------------|
| Observe | every loop | `GET /agents/{name}` → is it there, and does it match? |
| Create | agent missing | `POST /agents` |
| Update | agent differs | `POST /agents/{name}/versions` (new version) |
| Delete | object removed | `DELETE /agents/{name}` |

Observe drives everything: if the agent is gone it triggers Create, if it drifted
it triggers Update. Crossplane handles the delete-on-teardown finalizer for you.

## Auth

The provider pod uses workload identity to get an Entra token for
`https://ai.azure.com/` — the same keyless approach the agent itself uses. No
secret to store.

## Status

Implemented and verified end-to-end (create → self-heal → finalizer delete) against
a live Foundry project. Files:

- `apis/v1alpha1/` — the `FoundryAgent` type + managed-resource methods
- `internal/clients/foundry.go` — the REST client (Get / Upsert / Delete)
- `internal/controller/foundryagent/foundryagent.go` — connector + Observe/Create/Update/Delete
- `cmd/provider/main.go` — the manager entrypoint
- `config/crd/` — generated CRD

## Run it

```bash
cd crossplane
kubectl apply -f config/crd/                 # install the CRD
go run ./cmd/provider                        # run out-of-cluster (uses your az login)
kubectl apply -f ../examples/foundryagent.yaml
```

Out-of-cluster, the provider authenticates with `DefaultAzureCredential` (your
`az login`). Packaged as a provider image in-cluster, it uses workload identity —
same code, no secret either way.
