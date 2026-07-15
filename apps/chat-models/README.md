# chat-models — Chat with Foundry models on OpenChoreo

A single web service that serves a browser chat UI with a **model picker** for three
Azure AI Foundry model deployments. Pick a model, chat, and the conversation history is
maintained across turns. Built as one OpenChoreo `Component` (a `web-application`).

## What it does

- `GET /` — inline HTML+JS chat UI (no framework, plain `fetch`). A `<select>` offers the
  three model deployment names; a chat box holds the running conversation.
- `POST /chat` — body `{model, message, conversation_id?}` → `{reply, conversation_id}`.
- `GET /healthz` — liveness + the configured model list.

### The model picker

The three options are the deployment names injected as `MODEL_1`, `MODEL_2`, `MODEL_3`
(defaults `gpt-5-mini`, `gpt-5-nano`, `gpt-5.1`). They are three deployments on the **same**
Foundry account/endpoint — only the `model=` argument to the Responses call differs. The
selected value is sent with every `/chat` request; the backend rejects any model not in the
configured set.

### Conversation handling

On the first message the backend calls `client.conversations.create()` and gets a durable
`conversation_id`. It returns that id to the browser, which keeps it in a JS variable for the
session and echoes it back on every subsequent turn. Each `responses.create(...)` call passes
`conversation=<id>`, so Foundry stores and replays the turn history server-side — the app
never assembles history manually. The id lives client-side, so each browser tab is its own
conversation; a page reload starts a fresh one. Switching the model mid-conversation reuses
the same conversation, so context carries across models.

## Authentication (Entra / service principal)

No API keys. The app uses `azure-identity`'s `DefaultAzureCredential`, which reads a service
principal from the environment:

- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`
- `AZURE_CLIENT_SECRET`

`AIProjectClient(endpoint=FOUNDRY_PROJECT_ENDPOINT, credential=DefaultAzureCredential())`
then `.get_openai_client()` returns an authenticated `AzureOpenAI` client wired to the
project's `/openai/v1` surface. The SDK handles the token exchange (Entra token, scope
`https://ai.azure.com/.default`). The service principal needs an RBAC role on the Foundry
resource that permits inference (e.g. **Cognitive Services OpenAI User** / **Azure AI User**).

## Configuration

| Env var | Purpose | Default |
|---|---|---|
| `FOUNDRY_PROJECT_ENDPOINT` | Foundry project endpoint | — (required) |
| `MODEL_1` / `MODEL_2` / `MODEL_3` | The three deployment names | `gpt-5-mini` / `gpt-5-nano` / `gpt-5.1` |
| `PORT` | Listen port | `8080` |
| `AZURE_CLIENT_ID` / `AZURE_TENANT_ID` / `AZURE_CLIENT_SECRET` | SP creds for Entra | — (from secret store) |

See `.env.example`.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in the SP values
set -a && . ./.env && set +a
python app.py          # or: uvicorn app:app --host 0.0.0.0 --port 8080
# open http://localhost:8080
```

## Build the image

```bash
docker build -t <registry>/chat-models:latest .
docker push <registry>/chat-models:latest
```

## Deploy on OpenChoreo

Manifests live in `openchoreo/`:

- `component.yaml` — the `Component` (`web-application`, `autoDeploy: true`).
- `workload.yaml` — the BYO-image `Workload`; exposes an HTTP endpoint on `8080` and declares
  the three model Resources as dependencies whose resolved deployment names bind to
  `MODEL_1/2/3` (and the shared endpoint to `FOUNDRY_PROJECT_ENDPOINT`).
- `model-resources.yaml` — the three `Resource`s (`ClusterResourceType: azure-foundry-model`).
- `bindings.yaml` — the `ReleaseBinding` (env `development`) plus three
  `ResourceReleaseBinding` stubs.

```bash
kubectl apply -f openchoreo/component.yaml
kubectl apply -f openchoreo/model-resources.yaml
kubectl apply -f openchoreo/workload.yaml
kubectl apply -f openchoreo/bindings.yaml
```

Set the real image in `workload.yaml` (replace `<registry>/chat-models:latest`). After the
Component/Resources reconcile, fill the placeholders in `bindings.yaml`:

- `ReleaseBinding.spec.releaseName` ← the component's auto-cut `Release` name.
- each `ResourceReleaseBinding.spec.resourceRelease` ← that Resource's
  `status.latestRelease.name`.

### Wiring the service principal (secrets)

The SP env vars must come from the **platform secret store**, not from `workload.yaml`
in plaintext. The exact mechanism depends on the `SecretReference` type installed in your
OpenChoreo platform, so the block is left commented in `workload.yaml`. Wiring steps:

1. Store the three SP values in your platform secret backend (e.g. a `SecretReference`/
   ExternalSecret named `chat-models-sp` with keys `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
   `AZURE_CLIENT_SECRET`).
2. In `workload.yaml`, uncomment the three env entries and point each `valueFrom.secretRef`
   at that secret's name/key (adjust field names to your SecretReference schema).
3. Redeploy. `DefaultAzureCredential` picks the vars up automatically at startup.

## Notes / things to verify against your tenant

- **SDK versions** (`requirements.txt`): pinned to `azure-ai-projects==2.3.0`,
  `azure-identity==1.19.0`, `openai==2.2.0`. `get_openai_client()` returning `AzureOpenAI`
  (with `.conversations`/`.responses`) is the current 2.x behavior — confirm against the
  version you install. `openai` is normally a transitive dep of `azure-ai-projects`; it is
  pinned here only to guarantee the Conversations API is present.
- **Conversations + Responses API** is relatively new on Azure Foundry (v1/preview surface).
  If `conversations.create()` is not enabled for your resource/region, fall back to Responses
  **chaining**: pass `previous_response_id=<prior response.id>` on each `responses.create`
  (thread that id client-side instead of a conversation id). The UI/endpoint contract stays
  the same.
- **Model versions** in `model-resources.yaml` (`2025-08-07`, `2025-11-13`) are placeholders —
  set them to versions actually available in your Foundry account/region.
- `output_text` is a convenience aggregation on the Responses result; it is populated for
  plain text replies (what this UI uses).

## Files

```
apps/chat-models/
├── app.py                       # FastAPI app + inline chat UI
├── requirements.txt
├── Dockerfile
├── .env.example
├── README.md
└── openchoreo/
    ├── component.yaml
    ├── workload.yaml
    ├── model-resources.yaml
    └── bindings.yaml
```
