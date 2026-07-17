# Rebuild the azure-poc demo

Grounded in what actually ran last time. Two things to know up front:

- **Applied with `kubectl`, not MCP.** The `openchoreo-cp` MCP server was pointed at a
  different cluster, so every object below was `kubectl apply`'d directly. If MCP now
  targets this cluster you can use the developer-skill tools instead — same objects.
- **No gateway patching.** Public reach is just `endpoints.<name>.visibility: [external]`
  on the Workload — that's what puts it on the ingress gateway. Without it you'd be
  port-forwarding. (Release/ProjectReleaseBinding names are auto-generated — don't hardcode.)

## Fixed values

- Foundry project endpoint (`EP`): `https://<your-account>.services.ai.azure.com/api/projects/<your-project>`
- Model deployments (must already exist in Foundry — the `-ref` type references them): `gpt-5-mini`, `gpt-5-nano`, `gpt-5.1`
- Images (ttl.sh, ~4h TTL — rebuild if expired): `ttl.sh/chat-models-rashad-md2:4h`, `ttl.sh/agent-mcp-chat-rashad-md3:4h`

## Auth (decide at rebuild time)

- **Interim** (what worked): bake an `az` token (`az account get-access-token --resource
  https://ai.azure.com`) into each Workload as `AZURE_AI_TOKEN`; run the Crossplane
  provider out-of-cluster with `az login`. Token lasts ~1h — reinject on expiry.
- **Ideal** (needs the SP): SecretReference with `AZURE_CLIENT_ID/TENANT/SECRET`; provider
  in-cluster via workload identity; ASO for models instead of `-ref`.

## Steps

1. **Base up:** cluster + OpenChoreo installed; `default` project type, `default` pipeline,
   and dev/staging/prod environments exist.
2. **ResourceTypes:** `kubectl apply -f resourcetypes/`.
3. **Crossplane provider** (agent lifecycle):
   `kubectl apply -f crossplane/config/crd/ -f crossplane/config/agent-rbac.yaml`,
   then `az login` and `cd crossplane && go run ./cmd/provider` (out-of-cluster, interim).
   `agent-rbac.yaml` lets the data-plane agent apply the FoundryAgent CRs.
4. **Project** `azure-poc` (type `default`, pipeline `default`) + its dev ProjectReleaseBinding
   (this cuts the cell namespace).
4a. **PE wires the Foundry account once per environment** — a `foundry-account` ConfigMap
   (keys `projectEndpoint`, `accountEndpoint`, `accountArmId`) in **both** the cell namespace
   (`dp-default-azure-poc-development-…`, for the model/agent `endpoint` outputs) **and** the
   provider namespace (`provider-foundry`, so the Crossplane provider can read the endpoint).
   The ResourceTypes reference it — so developers are prompted for *nothing* Azure.
5. **Resources** (`owner.projectName: azure-poc`) — developer supplies only these:
   - `model-mini` / `model-nano` / `model-51` → `azure-foundry-model-ref`, `modelName:` `gpt-5-mini` / `gpt-5-nano` / `gpt-5.1`
   - `mcp-agent` → `azure-foundry-prompt-agent-xp`, `modelDeploymentName: gpt-5.1`,
     `mcpServers: [{serverLabel: api-specs, serverUrl: https://gitmcp.io/Azure/azure-rest-api-specs}]`,
     instructions "use the api-specs MCP tools … cite the file/path".
6. **ResourceReleaseBinding per resource, env `development`** — **no `resourceTypeEnvironmentConfigs`**
   (account details come from the `foundry-account` ConfigMap). Just pin the cut `resourceRelease`.
   Wait for all four `Ready=True`; the agent's FoundryAgent CR shows `SYNCED/READY=True` (its
   `projectEndpoint` is empty — the provider reads it from the ConfigMap).
7. **Two apps** — Component (`deployment/web-application`, `autoDeploy: true`) + Workload:
   - **chat-models** — image `chat-models-rashad-md2`; deps
     `model-mini → {deploymentName: MODEL_1, endpoint: FOUNDRY_PROJECT_ENDPOINT}`,
     `model-nano → {deploymentName: MODEL_2}`, `model-51 → {deploymentName: MODEL_3}`;
     endpoint `http` port 8080 `visibility: [external]`; env `AZURE_AI_TOKEN`.
   - **agent-mcp-chat** — image `agent-mcp-chat-rashad-md3`; dep
     `mcp-agent → {agentName: AGENT_NAME}`; env `FOUNDRY_PROJECT_ENDPOINT: <EP>`, `AZURE_AI_TOKEN`;
     endpoint `http` port 8080 `visibility: [external]`.
   `autoDeploy` cuts + binds a ComponentRelease in development on each Workload apply.
8. **Reinject token** (interim) whenever it expires: fetch a fresh `az` token, patch the
   `AZURE_AI_TOKEN` env on both Workloads, re-apply (auto-deploy rolls the pods).
9. **Verify** at the gateway (host from `ReleaseBinding.status.endpoints[].externalURLs`,
   port 19080): `chat-models` `/chat/stream` returns a model reply; `agent-mcp-chat` `/tools`
   lists the `api-specs` server and a tool-using turn streams `tools_listed` + `tool` frames.

## Gotchas

- **Web apps need `visibility: [external]`** or the gateway URL 404s. This is the only "gateway" step.
- **Cluster DNS uses UDP.** If pods can't resolve Foundry (`NameResolutionError`) it's node
  egress (VPN/MTU), not the token — fix node DNS or force CoreDNS to TCP.
- **Provider run-mode:** out-of-cluster needs a live `az login`; if FoundryAgent CRs sit
  un-SYNCED, the provider isn't running.
- **external-agent-telemetry stays parked** until the admin registers
  `Microsoft.OperationalInsights` + `Microsoft.Insights` on the subscription.
