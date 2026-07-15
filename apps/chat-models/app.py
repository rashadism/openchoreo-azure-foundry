"""
Chat with Models — a tiny FastAPI web service for an OpenChoreo + Azure AI Foundry demo.

The browser UI lets a user pick one of three Foundry model deployments and chat with it,
with conversation history maintained across turns via the Azure OpenAI Responses +
Conversations APIs.

Auth: Microsoft Entra via azure-identity DefaultAzureCredential. At runtime a service
principal is read from AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_CLIENT_SECRET; the
azure-ai-projects SDK exchanges it for a token (scope https://ai.azure.com/.default)
when it builds the OpenAI client.
"""
import os
from functools import lru_cache

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

# ---- Config from environment ------------------------------------------------
PROJECT_ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
# The 3 model deployment names are injected by OpenChoreo from the model Resources.
MODELS = [
    os.environ.get("MODEL_1", "gpt-5-mini"),
    os.environ.get("MODEL_2", "gpt-5-nano"),
    os.environ.get("MODEL_3", "gpt-5.1"),
]
PORT = int(os.environ.get("PORT", "8080"))

app = FastAPI(title="Chat with Models")


@lru_cache(maxsize=1)
def get_openai_client():
    """Build (once) an authenticated OpenAI client backed by the Foundry project.

    DefaultAzureCredential picks up the service principal from the environment.
    get_openai_client() returns an openai.AzureOpenAI configured against the
    project endpoint's /openai/v1 surface, so .responses and .conversations work.
    """
    if not PROJECT_ENDPOINT:
        raise RuntimeError("FOUNDRY_PROJECT_ENDPOINT is not set")
    project = AIProjectClient(
        endpoint=PROJECT_ENDPOINT,
        credential=DefaultAzureCredential(),
    )
    return project.get_openai_client()


# ---- API models -------------------------------------------------------------
class ChatRequest(BaseModel):
    model: str
    message: str
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    conversation_id: str


# ---- Endpoints --------------------------------------------------------------
@app.get("/healthz")
def healthz():
    return {"status": "ok", "models": MODELS}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if req.model not in MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model: {req.model}")
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Empty message")

    client = get_openai_client()

    # A Conversation is a durable, server-side store of turns. Create one on the
    # first message; the browser then echoes conversation_id back on every turn so
    # the Responses API can maintain history for us (no manual history plumbing).
    conversation_id = req.conversation_id
    if not conversation_id:
        conversation_id = client.conversations.create().id

    try:
        response = client.responses.create(
            model=req.model,
            conversation=conversation_id,
            input=req.message,
        )
    except Exception as exc:  # surface Azure/SDK errors to the UI
        raise HTTPException(status_code=502, detail=f"Foundry call failed: {exc}")

    return ChatResponse(reply=response.output_text, conversation_id=conversation_id)


@app.get("/", response_class=HTMLResponse)
def index():
    options = "\n".join(f'<option value="{m}">{m}</option>' for m in MODELS)
    return HTMLResponse(INDEX_HTML.replace("{{OPTIONS}}", options))


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Chat with Models</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; max-width: 760px; margin: 0 auto;
         padding: 16px; display: flex; flex-direction: column; height: 100vh;
         box-sizing: border-box; }
  header { display: flex; gap: 12px; align-items: center; margin-bottom: 12px; }
  h1 { font-size: 18px; margin: 0; flex: 1; }
  select, button, textarea { font: inherit; padding: 8px; border-radius: 8px;
         border: 1px solid #8886; }
  #log { flex: 1; overflow-y: auto; border: 1px solid #8884; border-radius: 12px;
         padding: 12px; display: flex; flex-direction: column; gap: 10px; }
  .msg { padding: 8px 12px; border-radius: 12px; max-width: 80%; white-space: pre-wrap; }
  .user { align-self: flex-end; background: #2563eb; color: #fff; }
  .bot  { align-self: flex-start; background: #8882; }
  .meta { font-size: 11px; opacity: 0.6; }
  form { display: flex; gap: 8px; margin-top: 12px; }
  textarea { flex: 1; resize: none; height: 44px; }
</style>
</head>
<body>
  <header>
    <h1>Chat with Models</h1>
    <label>Model: <select id="model">{{OPTIONS}}</select></label>
  </header>
  <div id="log"></div>
  <form id="form">
    <textarea id="input" placeholder="Type a message…" autofocus></textarea>
    <button type="submit" id="send">Send</button>
  </form>
<script>
  // Conversation id is kept client-side for this browser session and echoed to
  // /chat on every turn so history is maintained server-side by the Responses API.
  let conversationId = null;
  const log = document.getElementById('log');
  const form = document.getElementById('form');
  const input = document.getElementById('input');
  const send = document.getElementById('send');

  function add(text, cls, meta) {
    const el = document.createElement('div');
    el.className = 'msg ' + cls;
    el.textContent = text;
    if (meta) { const m = document.createElement('div'); m.className='meta'; m.textContent=meta; el.appendChild(m); }
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const message = input.value.trim();
    if (!message) return;
    const model = document.getElementById('model').value;
    add(message, 'user');
    input.value = '';
    send.disabled = true;
    const pending = add('…', 'bot', model);
    try {
      const res = await fetch('/chat', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ model, message, conversation_id: conversationId })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.statusText);
      conversationId = data.conversation_id;
      pending.firstChild ? pending.replaceChild(document.createTextNode(data.reply), pending.firstChild)
                         : pending.textContent = data.reply;
    } catch (err) {
      pending.textContent = 'Error: ' + err.message;
    } finally {
      send.disabled = false;
      input.focus();
    }
  });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORT)
