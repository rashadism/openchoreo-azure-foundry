# Sample apps

Three demo components showing how apps on OpenChoreo consume Azure AI Foundry. Each
is self-contained (app code + Dockerfile + OpenChoreo manifests + README) and ready to
deploy once a service principal is available. Nothing here is deployed or calls Azure
live yet.

| App | What it shows |
|-----|---------------|
| [`chat-models/`](./chat-models) | A chat UI with a **3-model picker**; depends on three `azure-foundry-model` Resources; conversation history via the Responses API. |
| [`agent-mcp-chat/`](./agent-mcp-chat) | A chat UI over a **Foundry agent wired to a public, tokenless MCP tool** (gitmcp.io). Includes an idempotent create-agent script. |
| [`external-agent-telemetry/`](./external-agent-telemetry) | An **agent hosted on OpenChoreo that publishes OpenTelemetry to Foundry** (App Insights → Observability). Secrets only, no resource dependency. |

## Shared conventions

- **Auth:** models and agents are reached with `DefaultAzureCredential`, which reads the
  service principal from `AZURE_CLIENT_ID` / `AZURE_TENANT_ID` / `AZURE_CLIENT_SECRET`
  (token scope `https://ai.azure.com/.default`). Agents are Entra-only; models also accept
  keys but these apps use Entra. Telemetry uses the App Insights connection string, not Entra.
- **Secrets:** the SP creds and endpoints are injected from a `SecretReference` / the
  platform secret store — the exact schema is platform-specific and left to wire at deploy
  time (see each app's README). No secrets are committed.
- **Deploy:** build the image, push it, set the Workload `image`, then apply the manifests
  in `openchoreo/`. Fill the `resourceRelease` / `releaseName` placeholders from the
  cut releases (`Resource.status.latestRelease.name` and the auto-cut `ComponentRelease`).

## Known gap

`agent-mcp-chat` creates its agent (with the MCP tool) via a script, because the
`azure-foundry-agent` ResourceType doesn't yet pass an `mcpServers` array through to
the `FoundryAgent` CR. Adding that passthrough is the productized path.
