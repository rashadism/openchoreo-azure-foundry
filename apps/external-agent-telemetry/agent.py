"""External agent hosted on OpenChoreo, observed by Azure AI Foundry.

Flow: this service emits OpenTelemetry spans that follow the GenAI semantic
conventions to the Application Insights resource connected to a Foundry project.
Foundry matches spans by `gen_ai.agent.id` and shows them in its Traces view once
the agent is registered in the portal (a one-time manual step, see README).

Three auth surfaces (see README):
  - client -> this agent : your own concern (not handled here; add your gateway/authz)
  - agent  -> telemetry  : Application Insights connection string (NOT Entra)
  - agent  -> model      : Entra / service principal via DefaultAzureCredential
"""
import os
import logging

from fastapi import FastAPI
from pydantic import BaseModel
from opentelemetry import trace

# --- Telemetry: export OTel spans to Application Insights -----------------------
# configure_azure_monitor() reads APPLICATIONINSIGHTS_CONNECTION_STRING but we pass
# it explicitly to be unambiguous. This wires the global tracer provider.
from azure.monitor.opentelemetry import configure_azure_monitor

# --- Model call: Foundry Responses API via Entra (service principal) -------------
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient

log = logging.getLogger("telemetry-agent")
logging.basicConfig(level=logging.INFO)

# --- Config (all from env; injected via SecretReferences on OpenChoreo) ----------
APPINSIGHTS_CONN = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
FOUNDRY_PROJECT_ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
MODEL_DEPLOYMENT = os.environ.get("MODEL_DEPLOYMENT", "gpt-4o-mini")
AGENT_ID = os.environ.get("AGENT_ID", "openchoreo-telemetry-agent-v1")
AGENT_NAME = os.environ.get("AGENT_NAME", "OpenChoreo Telemetry Agent")
PORT = int(os.environ.get("PORT", "8080"))
# GenAI semconv system identifier for Azure AI Foundry inference.
GEN_AI_SYSTEM = "az.ai.inference"

if APPINSIGHTS_CONN:
    configure_azure_monitor(connection_string=APPINSIGHTS_CONN)
    log.info("Azure Monitor OpenTelemetry configured; exporting spans to App Insights.")
else:
    # No connection string: spans still form but won't reach Foundry. Kept non-fatal
    # so /healthz works in environments without telemetry wired yet.
    log.warning("APPLICATIONINSIGHTS_CONNECTION_STRING not set; telemetry disabled.")

tracer = trace.get_tracer(__name__)

# Lazily-built OpenAI-compatible client for the Foundry Responses API.
_project_client = None
_openai_client = None


def get_openai_client():
    """Return an OpenAI-compatible client for the Foundry Responses API.

    AIProjectClient uses DefaultAzureCredential, which picks up the service
    principal env vars AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_CLIENT_SECRET and
    requests a token for scope https://ai.azure.com/.default.
    """
    global _project_client, _openai_client
    if _openai_client is None:
        if not FOUNDRY_PROJECT_ENDPOINT:
            raise RuntimeError("FOUNDRY_PROJECT_ENDPOINT is not set")
        _project_client = AIProjectClient(
            endpoint=FOUNDRY_PROJECT_ENDPOINT,
            credential=DefaultAzureCredential(),
        )
        _openai_client = _project_client.get_openai_client()
    return _openai_client


app = FastAPI(title="OpenChoreo -> Foundry Telemetry Agent")


class Invoke(BaseModel):
    message: str


@app.on_event("startup")
def emit_agent_creation_span():
    """Emit a one-shot span representing the agent at startup.

    Carries gen_ai.operation.name=create_agent and gen_ai.agent.id so Foundry can
    associate this process with the registered external-agent record.
    """
    with tracer.start_as_current_span("create_agent") as span:
        span.set_attribute("gen_ai.operation.name", "create_agent")
        span.set_attribute("gen_ai.system", GEN_AI_SYSTEM)
        span.set_attribute("gen_ai.agent.id", AGENT_ID)
        span.set_attribute("gen_ai.agent.name", AGENT_NAME)
        log.info("Emitted create_agent span for gen_ai.agent.id=%s", AGENT_ID)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "agent_id": AGENT_ID, "telemetry": bool(APPINSIGHTS_CONN)}


@app.post("/invoke")
def invoke(body: Invoke):
    """Run one agent turn and emit GenAI spans so the run shows up in Foundry."""
    # Outer span: the agent invocation. gen_ai.agent.id ties it to the registration.
    with tracer.start_as_current_span("invoke_agent") as agent_span:
        agent_span.set_attribute("gen_ai.operation.name", "invoke_agent")
        agent_span.set_attribute("gen_ai.system", GEN_AI_SYSTEM)
        agent_span.set_attribute("gen_ai.agent.id", AGENT_ID)
        agent_span.set_attribute("gen_ai.agent.name", AGENT_NAME)

        # Inner span: the model inference call.
        with tracer.start_as_current_span(f"chat {MODEL_DEPLOYMENT}") as model_span:
            model_span.set_attribute("gen_ai.operation.name", "chat")
            model_span.set_attribute("gen_ai.system", GEN_AI_SYSTEM)
            model_span.set_attribute("gen_ai.request.model", MODEL_DEPLOYMENT)

            client = get_openai_client()
            response = client.responses.create(
                model=MODEL_DEPLOYMENT,
                input=body.message,
            )

            # Record token usage if the provider returned it (GenAI semconv names).
            usage = getattr(response, "usage", None)
            if usage is not None:
                if getattr(usage, "input_tokens", None) is not None:
                    model_span.set_attribute(
                        "gen_ai.usage.input_tokens", usage.input_tokens
                    )
                if getattr(usage, "output_tokens", None) is not None:
                    model_span.set_attribute(
                        "gen_ai.usage.output_tokens", usage.output_tokens
                    )

            text = getattr(response, "output_text", None) or ""
            model_span.set_attribute("gen_ai.response.model", MODEL_DEPLOYMENT)

        return {"agent_id": AGENT_ID, "reply": text}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
