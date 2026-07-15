# external-agent-telemetry

A self-hosted AI agent that **runs on OpenChoreo** and is **observed by Azure AI
Foundry**. Foundry acts as the observability / control plane; the agent runs on
your own compute (an OpenChoreo `deployment/service` component) and streams
OpenTelemetry traces to Foundry's connected Application Insights resource. The
runs then appear in Foundry's **Observability > Traces** view (and, once the agent
is registered, in the agent-scoped Traces tab).

> Foundry stores only **registration metadata** for external agents. It does not
> host, proxy, or invoke this runtime — OpenChoreo does. Telemetry is the only
> thing that flows to Foundry.

## Concept: Foundry as control plane for an OpenChoreo-hosted agent

```
   client ──HTTP──> [ telemetry-agent on OpenChoreo ] ──Responses API──> Foundry model
                             │
                             │  OpenTelemetry spans (GenAI semconv, gen_ai.agent.id)
                             ▼
                   [ Application Insights ]  ── connected to ──>  [ Foundry project ]
                                                                   Observability > Traces
```

The agent emits spans that follow the **OpenTelemetry GenAI semantic
conventions**. Two span shapes are produced:

- **startup** — a `create_agent` span (`gen_ai.operation.name = "create_agent"`)
  carrying `gen_ai.agent.id = <AGENT_ID>`, representing the agent itself.
- **per request** — `POST /invoke` produces an `invoke_agent` span
  (`gen_ai.operation.name = "invoke_agent"`) wrapping an inner model-call span
  (`gen_ai.operation.name = "chat"`, `gen_ai.request.model`, and
  `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` when the provider
  returns them). Every span carries `gen_ai.agent.id` so Foundry can attribute it
  to the registered agent.

## OTel → App Insights → Foundry flow

1. `configure_azure_monitor(connection_string=...)` (from `azure-monitor-opentelemetry`)
   wires the global OTel tracer provider to an Azure Monitor exporter.
2. Spans are exported to the **Application Insights resource connected to your
   Foundry project**.
3. Foundry reads that App Insights resource and, matching on
   `gen_ai.agent.id == otel_agent_id`, surfaces the traces in the portal.
   Ingestion latency is typically 2–5 minutes.

## Three auth surfaces

| Hop | What authenticates | How |
| --- | --- | --- |
| client → **this agent** | your own concern | Not implemented here. Put your own gateway / authz in front of `/invoke`. |
| agent → **telemetry** (App Insights) | `APPLICATIONINSIGHTS_CONNECTION_STRING` | **Connection string only — NOT Entra.** No service principal is needed to emit traces. |
| agent → **model** (Foundry Responses API) | Entra / service principal | `DefaultAzureCredential` reads `AZURE_CLIENT_ID` / `AZURE_TENANT_ID` / `AZURE_CLIENT_SECRET`; token scope `https://ai.azure.com/.default`. |

## Manual Foundry-portal registration step

Registration is a **one-time portal action** — it is intentionally NOT automated
here (no live Azure calls are made by this repo).

> **Important distinction (verify for your tenant).** Foundry has two related
> features and the docs use overlapping language:
>
> - **External agents** (observability + evaluation): your agent keeps its own
>   endpoint and shares **only OpenTelemetry telemetry**. **No AI Gateway** is
>   required. You register by supplying just a name, description, and the
>   **OpenTelemetry agent ID** (`otel_agent_id`). This is the path this component
>   targets. Preview: create/update requires header
>   `Foundry-Features: ExternalAgents=V1Preview` (SDK: `AIProjectClient(..., allow_preview=True)`).
> - **Control Plane custom agents**: route live traffic **through an AI Gateway**,
>   so registration additionally asks for the **agent URL + protocol** (and the
>   gateway config). Use this only if you want Foundry to proxy invocations.
>
> The task brief mentions "AI Gateway + agent URL + OTel agent ID". Those come
> from *different* registration flows — pick based on whether you want pure
> observability (external agent, OTel ID only) or gateway-proxied traffic (custom
> agent, + URL/protocol/gateway). Confirm the current portal wording, since both
> features are in preview and evolving.

**Observability-only (external agent) registration, portal path:**

1. Open the Foundry portal (https://ai.azure.com), select your project.
2. **Build > Agents > New agent > Link external agent**.
3. Enter the agent **name**, **description**, and the **OpenTelemetry ID**. Set the
   OpenTelemetry ID to the **same value** as this service's `AGENT_ID`
   (e.g. `openchoreo-telemetry-agent-v1`) so spans match the registration.

Equivalent SDK form (for reference — do not run here):

```python
project.agents.create_version(
    agent_name="openchoreo-telemetry-agent",
    description="Agent hosted on OpenChoreo.",
    definition=ExternalAgentDefinition(otel_agent_id="openchoreo-telemetry-agent-v1"),
)
```

Prereqs: the Foundry project must have an **Application Insights resource
connected** (Management > Connected resources), and your identity needs the
**Foundry User** role plus Reader/Monitoring Reader on App Insights.

## How secrets are wired on OpenChoreo (no Resource dependency)

Per the design, this component has **no `dependencies.resources[]`**. There is no
managed-infrastructure Resource to bind. Instead, every Azure endpoint/token is
injected as an env var sourced from the **platform secret store via a
SecretReference**:

- `APPLICATIONINSIGHTS_CONNECTION_STRING`
- `FOUNDRY_PROJECT_ENDPOINT`
- `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_SECRET`

(`MODEL_DEPLOYMENT`, `AGENT_ID`, `AGENT_NAME`, `PORT` are non-secret and set as
plain env values.)

In `openchoreo/workload.yaml` these are shown using a `valueFrom.secretRef`
pointing at a SecretReference named `telemetry-agent-secrets`.

> **Uncertainty — verify against your OpenChoreo version.** The exact schema for
> referencing a `SecretReference` from a Workload env var is **not asserted here
> with confidence**. The `valueFrom.secretRef` shape in `workload.yaml` is
> illustrative. Depending on your platform build the real mechanism may be a
> top-level `SecretReference` custom resource that the Workload references by
> name, an `envFrom`-style block, or a platform-specific secret-store binding.
> Create the `SecretReference` (holding the five secret keys) with your platform
> engineer and adjust the Workload's env wiring to match the supported field.

## Files

- `agent.py` — FastAPI service: startup `create_agent` span + `POST /invoke`
  (GenAI-instrumented Responses API call) + `GET /healthz`.
- `requirements.txt` — pinned deps (`azure-monitor-opentelemetry`,
  `opentelemetry-sdk`, `azure-ai-projects`, `azure-identity`, `fastapi`, `uvicorn`).
- `Dockerfile` — `python:3.12-slim`, non-root, runs `python agent.py`.
- `.env.example` — all config/secrets with placeholders.
- `openchoreo/component.yaml` — `Component` (`deployment/service`, `autoDeploy`).
- `openchoreo/workload.yaml` — `Workload` (container image, env, secret refs, HTTP
  endpoint `:8080`, no resource deps).
- `openchoreo/releasebinding.yaml` — `ReleaseBinding` to `development` (Active).
- `README.md` — this file.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in real values
set -a; . ./.env; set +a
python agent.py
# then:
curl -s localhost:8080/healthz
curl -s -X POST localhost:8080/invoke -H 'content-type: application/json' \
  -d '{"message":"hello"}'
```

## Sources / references

- Register external agents for observability and evaluation — Microsoft Learn:
  https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/register-external-agent
- Configure tracing for AI agent frameworks — Microsoft Learn:
  https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/trace-agent-framework
- Register and manage Control Plane custom agents (AI Gateway path):
  https://learn.microsoft.com/en-us/azure/ai-foundry/control-plane/register-custom-agent
- OpenTelemetry GenAI semantic conventions:
  https://opentelemetry.io/docs/specs/semconv/gen-ai/
- Responses API + `DefaultAzureCredential` (scope `https://ai.azure.com/.default`):
  https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses

### Notes / uncertainties

- `azure-ai-projects` `get_openai_client()` returns an OpenAI-compatible client;
  the Responses API surface (`client.responses.create`) and the `usage.input_tokens`
  / `usage.output_tokens` fields are used defensively (guarded with `getattr`) in
  case a given deployment/SDK version returns a different shape.
- Package version pins in `requirements.txt` are chosen as recent stable releases;
  bump to match your environment. `azure-ai-projects` external-agent *registration*
  APIs require newer preview builds, but this service only *emits* telemetry and
  calls the Responses API, so it does not need the registration APIs.
- `gen_ai.system` is set to `az.ai.inference`; the exact recommended value can vary
  across semconv revisions — it does not affect trace-to-agent matching (that is
  driven solely by `gen_ai.agent.id`).
```
