# agent-mcp-chat

A demo web component: a small chat UI that talks to an **Azure AI Foundry _agent_**
which has a **public, tokenless MCP tool** attached. The agent uses the MCP tool to
answer questions about the Azure REST API specifications.

- **Foundry project endpoint:** `https://rashad-4421-resource.services.ai.azure.com/api/projects/rashad-4421`
- **Model deployment:** `gpt-5-mini`
- **MCP tool:** `https://gitmcp.io/Azure/azure-rest-api-specs` (label `api-specs`, `require_approval: never`)

## What this demonstrates

1. Creating a **Foundry agent** (a declarative agent = model + instructions
   + tools, run by Foundry) with an **MCP** tool in its `tools` array.
2. A **FastAPI** web app that chats with that agent through the **Responses API**,
   using an `agent_reference` plus a **conversation** object for multi-turn history.
3. Authenticating to Foundry with **Entra ID only** (agents have no API key).

## The agent + the MCP tool

Foundry lets an agent call **remote MCP servers** as tools. Each tool needs
a unique `server_label` and a `server_url`. The tool object (inside
`definition.tools`):

```json
{
  "type": "mcp",
  "server_label": "api-specs",
  "server_url": "https://gitmcp.io/Azure/azure-rest-api-specs",
  "require_approval": "never"
}
```

### Why gitmcp.io (the public tokenless choice)

`gitmcp.io` turns any public GitHub repo into a **remote MCP server over HTTPS**
with **no authentication / no token** — you just point `server_url` at
`https://gitmcp.io/<owner>/<repo>`. That makes it ideal for a demo: there is no
secret to provision for the tool itself, and it is exactly the server used in
Microsoft's own Foundry MCP quickstart. We use `Azure/azure-rest-api-specs`, so
the agent can look up Azure REST API resource schemas and api-versions.

Other public tokenless options you could swap in via `MCP_SERVER_URL`: any other
`gitmcp.io/<owner>/<repo>`, or Microsoft Learn's public docs MCP
(`https://learn.microsoft.com/api/mcp`). Authenticated MCP servers instead need a
Foundry **project connection** (`project_connection_id` on the tool) — out of
scope for this tokenless demo.

### `require_approval: never`

With `never`, the agent **auto-approves** MCP tool calls. That avoids the
interactive approval handshake — where a response comes back with an
`mcp_approval_request` output item that you must answer with an
`mcp_approval_response` (via `previous_response_id`) before the tool runs. Because
we set `never`, `app.py` does not implement that handshake. In production you'd
typically use `always` (or a per-tool allow-list) and build an approval UX.

## Auth (Entra-only)

Foundry **agents are Entra-only — there is no API key.** Both the create script
and the web app use `azure-identity`'s `DefaultAzureCredential` and request a
token for scope **`https://ai.azure.com/.default`**, then send it as
`Authorization: Bearer <token>`.

`DefaultAzureCredential` picks up a **service principal** from the standard env
vars `AZURE_CLIENT_ID` / `AZURE_TENANT_ID` / `AZURE_CLIENT_SECRET` (how it runs in
the platform), or falls back to your `az login` session for local dev. The SP
needs a Foundry role on the project that permits creating agents and creating
responses (e.g. an Azure AI user/developer role on the project).

## Conversation handling

The Responses API is OpenAI-compatible. We maintain multi-turn history with a
**conversation object** created on the project endpoint:

1. `POST {endpoint}/openai/v1/conversations` → returns `{"id": "conv_..."}`.
2. Each turn: `POST {endpoint}/openai/v1/responses` with the `agent_reference`,
   the `conversation` id, and just the new user turn in `input`. Foundry appends
   the agent's output to the conversation, so history persists server-side.

```json
{
  "agent_reference": {"type": "agent_reference", "name": "mcp-agent"},
  "conversation": "conv_123",
  "input": [{"role": "user", "content": "What api-versions exist for Microsoft.Storage/storageAccounts?"}]
}
```

The browser stores the returned `conversation_id` and sends it back on each
`/chat` call. (An alternative to a conversation object is chaining
`previous_response_id` between responses; we use the conversation object because
it maps cleanly to a chat session.)

> Field-name note / uncertainty: the current Foundry docs are not fully
> consistent on the invocation field name. The **prompt-agent quickstart** uses
> `agent_reference` (top-level), the Python SDK binds the agent via
> `get_openai_client(agent_name=...)`, and the **MCP how-to** curl uses `agent`.
> This app follows the quickstart and sends **`agent_reference`**. If your project
> rejects it, try the key `agent` with the same object, or bind the agent through
> the `azure-ai-projects` SDK. `agent_reference` also accepts an optional
> `"version"` field to pin a specific agent version.

## Files

| File | Purpose |
|------|---------|
| `create_agent.py` | Idempotently create the prompt agent **with** the MCP tool (Python + DefaultAzureCredential). Treats HTTP 409 as success. |
| `create_agent.sh` | Same, as a `curl` script (uses `az account get-access-token`). |
| `app.py` | FastAPI app: `GET /` chat UI, `POST /chat`, `GET /healthz`. |
| `requirements.txt` | Pinned deps. |
| `Dockerfile` | `python:3.12-slim` image. |
| `.env.example` | Config template. |
| `openchoreo/*.yaml` | Component, Workload, optional agent Resource, ReleaseBinding. |

## Run it

### 1. Create the agent (once)

```bash
export FOUNDRY_PROJECT_ENDPOINT="https://rashad-4421-resource.services.ai.azure.com/api/projects/rashad-4421"
export AGENT_NAME="mcp-agent"
export FOUNDRY_MODEL_DEPLOYMENT="gpt-5-mini"

# Auth: either `az login`, or export AZURE_CLIENT_ID/TENANT_ID/CLIENT_SECRET.
python create_agent.py         # or: ./create_agent.sh
```

### 2. Run the web app locally

```bash
pip install -r requirements.txt
cp .env.example .env            # fill in creds (or rely on `az login`)
set -a; source .env; set +a
python app.py                   # serves on http://localhost:8080
```

`POST /chat` body `{ "message": "...", "conversation_id": "conv_..."? }` →
`{ "reply": "...", "conversation_id": "conv_..." }`. `GET /healthz` returns status.

### 3. Container

```bash
docker build -t <registry>/agent-mcp-chat:latest .
docker run -p 8080:8080 --env-file .env <registry>/agent-mcp-chat:latest
```

## Deploy on OpenChoreo

Manifests are in `openchoreo/`:

- `component.yaml` — `Component` (`deployment/web-application`, `autoDeploy: true`).
- `workload.yaml` — `Workload`: container image, `PORT`/`AGENT_NAME` env, an HTTP
  endpoint on `8080`, and an optional dependency on the agent Resource.
- `agent-resource.yaml` — optional agent `Resource` (see the gap note below).
- `releasebinding.yaml` — `ReleaseBinding` binding the auto-cut release into
  `development` with `state: Active`.

Apply/register them through your usual OpenChoreo flow (control-plane MCP /
`kubectl`). Set `<registry>` in `workload.yaml` to your image registry and fill
`releaseName` once `autoDeploy` cuts the release.

### Secrets

The service-principal creds (`AZURE_CLIENT_ID` / `AZURE_TENANT_ID` /
`AZURE_CLIENT_SECRET`) and `FOUNDRY_PROJECT_ENDPOINT` should **not** be inlined in
`workload.yaml`. Provide them through OpenChoreo's **`SecretReference`** /
platform secret store, which resolves a stored secret into the workload's env at
deploy time (the platform injects the referenced keys as container env vars).
Store the SP creds + endpoint in the platform secret store and reference them from
the workload's env; the exact `SecretReference` schema depends on your platform
install, so wire it to match your secret store rather than copying an invented
shape here. `AGENT_NAME` and `PORT` are plain (non-secret) env and stay inline.

### Known gap: `mcpServers` / MCP passthrough in the ResourceType

The optional agent `Resource` references `ClusterResourceType`
**`azure-foundry-agent`**. That ResourceType currently provisions a Foundry agent
from `agentName` + `modelDeploymentName` + `instructions` but **does not yet pass an
`mcpServers` / MCP array** down to the underlying Crossplane `FoundryAgent` CR. So a
Resource created from it today yields an agent **without** the MCP tool.

Because of that, this demo creates the agent **with** the MCP tool via
`create_agent.py`, and the Resource (if used) is only for lifecycle/binding of the
non-MCP definition. **Productized path:** add an `mcpServers` passthrough to the
`azure-foundry-agent` ResourceType and the Crossplane `FoundryAgent`
composition so the MCP tool array is declared in `agent-resource.yaml` and
provisioned by the platform — removing the need for the out-of-band script.

## Uncertainties / things to verify against your project

- **Invocation field name** (`agent_reference` vs `agent`) — see the note above.
- **api-version** for agent CRUD is `v1` (the Foundry projects "new" API); the
  Responses/conversations endpoints under `/openai/v1/...` are unversioned by
  query string. Confirm both resolve on your resource.
- **Response text extraction:** `app.py` reads `output_text`, falling back to
  concatenating `message`→`output_text` content parts. Adjust if your response
  shape differs.
- **gitmcp.io availability:** it's a public third-party service; for a reliable
  demo confirm it's reachable from the cluster's egress.
