# Rebuild the azure-poc demo

Ordered steps to recreate the live demo on a fresh cluster. The specs themselves
are regenerated with the openchoreo-developer / -platform-engineer skills — this is
just the sequence and the values that aren't obvious.

## Fixed values

- Foundry project endpoint: `https://rashad-4421-resource.services.ai.azure.com/api/projects/rashad-4421`
- Resource group (Owner): `rg-azure-foundry-openchoreo-rashad` · Subscription (Contributor): `corporate-rnd-001`
- Model deployments (must exist in Foundry for the `-ref` workaround): `gpt-5-mini`, `gpt-5-nano`, `gpt-5.1`
- Images (ttl.sh, ~4h TTL — rebuild if expired): `ttl.sh/chat-models-rashad-md2:4h`, `ttl.sh/agent-mcp-chat-rashad-md3:4h`

## Auth: pick one (decide at rebuild time)

- **Interim** (what worked): bake an `az` token into each Workload as `AZURE_AI_TOKEN`
  (scope `https://ai.azure.com`); run the Crossplane provider out-of-cluster with `az login`.
  Token expires ~1h — reinject (step 8).
- **Ideal** (needs the SP): SecretReference with `AZURE_CLIENT_ID/TENANT/SECRET`; provider
  in-cluster with workload identity; ASO for models instead of `-ref`.

## Steps

1. **Cluster + OpenChoreo up.** Control plane + data plane installed; `list_namespaces` works.
   Confirms base `default` ClusterProjectType, `default` DeploymentPipeline, and dev/staging/prod envs exist.
2. **Install the ResourceTypes:** `kubectl apply -f resourcetypes/`.
3. **Install the Crossplane provider** (for the agent): apply the CRD + RBAC, then run it.
   - `kubectl apply -f crossplane/config/crd/ -f crossplane/config/agent-rbac.yaml`
   - `az login`, then `cd crossplane && go run ./cmd/provider` (out-of-cluster, interim).
   - The RBAC (`openchoreo-foundryagent-access`) lets the data-plane agent apply FoundryAgent CRs.
4. **Create the project** `azure-poc` (type `default`, pipeline `default`), then its
   **ProjectReleaseBinding** for `development` — this cuts the cell namespace.
5. **Create the Resources** (all `owner.projectName: azure-poc`):
   - `model-mini` → `azure-foundry-model-ref`, `modelName: gpt-5-mini`
   - `model-nano` → `azure-foundry-model-ref`, `modelName: gpt-5-nano`
   - `model-51`   → `azure-foundry-model-ref`, `modelName: gpt-5.1`
   - `mcp-agent`  → `azure-foundry-prompt-agent-xp`, `modelDeploymentName: gpt-5.1`,
     `mcpServers: [{serverLabel: api-specs, serverUrl: https://gitmcp.io/Azure/azure-rest-api-specs}]`,
     instructions = "use the api-specs MCP tools … cite the file/path".
6. **Bind each Resource** in `development` (ResourceReleaseBinding), setting
   `resourceTypeEnvironmentConfigs`:
   - models (`-ref`): `endpoint: <Foundry project endpoint>`
   - agent (`-xp`): `projectEndpoint: <Foundry project endpoint>`
   Wait for all four `READY=True` (the agent's FoundryAgent CR shows `SYNCED/READY=True`).
7. **Deploy the two apps** (Component `web-application`, autoDeploy on):
   - `chat-models`: image `chat-models-rashad-md2`; deps `model-mini→{deploymentName:MODEL_1,
     endpoint:FOUNDRY_PROJECT_ENDPOINT}`, `model-nano→{deploymentName:MODEL_2}`,
     `model-51→{deploymentName:MODEL_3}`; endpoint http/8080 `visibility:[external]`.
   - `agent-mcp-chat`: image `agent-mcp-chat-rashad-md3`; env `FOUNDRY_PROJECT_ENDPOINT=<endpoint>`;
     dep `mcp-agent→{agentName:AGENT_NAME}`; endpoint http/8080 `visibility:[external]`.
   Both Workloads also carry `AZURE_AI_TOKEN` (interim). Cutting the Workload auto-binds a
   ComponentRelease in development.
8. **(Interim) Inject a fresh token** into both Workloads whenever it expires:
   `TOKEN=$(az account get-access-token --resource https://ai.azure.com --query accessToken -o tsv)`
   then patch the `AZURE_AI_TOKEN` env on each Workload and re-apply (auto-deploy rolls the pods).
9. **Verify** through the gateway (host from `ReleaseBinding.status.endpoints[].externalURLs`,
   port 19080): `chat-models` `/chat/stream` returns a model reply; `agent-mcp-chat` `/tools`
   lists the `api-specs` server and a tool-using turn streams `tools_listed` + `tool` frames.

## Gotchas

- **Web endpoints need `visibility:[external]`** or the gateway URL 404s.
- **Cluster DNS uses UDP.** If external resolution fails (`NameResolutionError`), it's node
  egress (VPN/MTU), not the token — CoreDNS `force_tcp` or fixing node DNS resolves it.
- **Provider run-mode.** Out-of-cluster needs a live `az login` on the host; in-cluster needs
  workload identity. If FoundryAgent CRs sit un-SYNCED, the provider isn't running.
- **external-agent-telemetry stays parked** until the admin registers
  `Microsoft.OperationalInsights` + `Microsoft.Insights` on the subscription (App Insights).
