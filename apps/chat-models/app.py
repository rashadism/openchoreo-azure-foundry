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
import json
import os
import time
from functools import lru_cache

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from azure.ai.projects import AIProjectClient
from azure.core.credentials import AccessToken
from azure.identity import DefaultAzureCredential


class _StaticToken:
    """Use a pre-issued bearer token when AZURE_AI_TOKEN is set (e.g. an `az` token).
    Interim path for testing before a service principal is available."""

    def __init__(self, token: str):
        self._t = token

    def get_token(self, *scopes, **kwargs):
        return AccessToken(self._t, int(time.time()) + 3000)


def _credential():
    tok = os.environ.get("AZURE_AI_TOKEN")
    return _StaticToken(tok) if tok else DefaultAzureCredential()

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
        credential=_credential(),
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


def _sse(obj: dict) -> str:
    """Encode one Server-Sent Event frame carrying a JSON payload."""
    return f"data: {json.dumps(obj)}\n\n"


@app.post("/chat/stream")
def chat_stream(req: ChatRequest):
    """Streaming counterpart of /chat.

    Returns text/event-stream. Frames are `data: <json>\\n\\n` where the json is one of:
      {"conversation_id": "..."}  – sent first so the client can keep threading history
      {"delta": "..."}            – an incremental text token as it arrives
      {"error": "..."}            – a failure (surfaced inline in the UI)
      {"done": true}              – the turn finished cleanly
    """
    if req.model not in MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model: {req.model}")
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Empty message")

    client = get_openai_client()

    # Create the Conversation up front (same semantics as /chat) so we can hand the
    # id back to the browser before any tokens flow. Failures here become an HTTP
    # error the fetch() sees before the stream opens.
    conversation_id = req.conversation_id
    if not conversation_id:
        try:
            conversation_id = client.conversations.create().id
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Foundry call failed: {exc}")

    def gen():
        # Announce the conversation id first so history threads even if we error later.
        yield _sse({"conversation_id": conversation_id})
        try:
            stream = client.responses.create(
                model=req.model,
                conversation=conversation_id,
                input=req.message,
                stream=True,
            )
            for event in stream:
                # The Responses API emits typed events; we only forward text deltas.
                if getattr(event, "type", None) == "response.output_text.delta":
                    delta = getattr(event, "delta", "")
                    if delta:
                        yield _sse({"delta": delta})
        except Exception as exc:  # emit the error inline instead of tearing the stream
            yield _sse({"error": f"Foundry call failed: {exc}"})
            return
        yield _sse({"done": True})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering so tokens flush live
            "X-Conversation-Id": conversation_id,
        },
    )


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
  :root {
    color-scheme: light;
    --border: color-mix(in srgb, currentColor 14%, transparent);
    --muted: color-mix(in srgb, currentColor 55%, transparent);
    --bot-bg: color-mix(in srgb, currentColor 7%, transparent);
    --field-bg: color-mix(in srgb, currentColor 4%, transparent);
    --accent: #2563eb;
    --accent-fg: #ffffff;
    --danger: #dc2626;
    --radius: 14px;
  }
  * { box-sizing: border-box; }
  body {
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    font-size: 15px; line-height: 1.5;
    max-width: 780px; margin: 0 auto; padding: 16px;
    display: flex; flex-direction: column; height: 100vh;
  }
  header {
    display: flex; gap: 12px; align-items: center;
    padding-bottom: 12px; margin-bottom: 12px;
    border-bottom: 1px solid var(--border);
  }
  h1 { font-size: 16px; font-weight: 600; margin: 0; flex: 1; letter-spacing: -0.01em; }
  .field { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--muted); }
  select {
    font: inherit; font-size: 13px; padding: 6px 8px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--field-bg); color: inherit;
  }
  #log {
    flex: 1; overflow-y: auto; padding: 4px 2px;
    display: flex; flex-direction: column; gap: 14px;
    scroll-behavior: smooth;
  }
  #log:empty::before {
    content: "Pick a model and say hello.";
    margin: auto; color: var(--muted); font-size: 14px;
  }
  .row { display: flex; flex-direction: column; gap: 4px; max-width: 82%; }
  .row.user { align-self: flex-end; align-items: flex-end; }
  .row.bot  { align-self: flex-start; align-items: flex-start; }
  .meta { font-size: 11px; color: var(--muted); padding: 0 4px; }
  .bubble {
    padding: 9px 13px; border-radius: var(--radius);
    white-space: pre-wrap; word-break: break-word; overflow-wrap: anywhere;
  }
  .user .bubble { background: var(--accent); color: var(--accent-fg); border-bottom-right-radius: 5px; }
  .bot  .bubble { background: var(--bot-bg); border: 1px solid var(--border); border-bottom-left-radius: 5px; }
  .bot  .bubble.error { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 45%, transparent); }
  .typing { display: inline-flex; gap: 4px; align-items: center; padding: 3px 0; }
  .typing span {
    width: 6px; height: 6px; border-radius: 50%; background: var(--muted);
    animation: blink 1.4s infinite both;
  }
  .typing span:nth-child(2) { animation-delay: 0.2s; }
  .typing span:nth-child(3) { animation-delay: 0.4s; }
  @keyframes blink { 0%, 80%, 100% { opacity: 0.25; } 40% { opacity: 1; } }
  .caret {
    display: inline-block; width: 2px; height: 1.05em; margin-left: 1px;
    background: currentColor; vertical-align: text-bottom;
    animation: caret 1.05s step-end infinite;
  }
  @keyframes caret { 50% { opacity: 0; } }
  form {
    display: flex; gap: 8px; align-items: flex-end;
    margin-top: 12px; padding: 8px;
    border: 1px solid var(--border); border-radius: var(--radius);
    background: var(--field-bg);
  }
  form:focus-within { border-color: color-mix(in srgb, var(--accent) 55%, var(--border)); }
  textarea {
    flex: 1; resize: none; font: inherit; color: inherit;
    border: 0; background: transparent; outline: none;
    padding: 6px; max-height: 160px; min-height: 24px;
  }
  button {
    font: inherit; font-weight: 600; font-size: 14px;
    padding: 8px 16px; border-radius: 10px; border: 0; cursor: pointer;
    background: var(--accent); color: var(--accent-fg);
  }
  button:disabled { opacity: 0.5; cursor: default; }
  .sr-only {
    position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
    overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
  }
</style>
</head>
<body>
  <header>
    <h1>Chat with Models</h1>
    <label class="field" for="model">Model
      <select id="model">{{OPTIONS}}</select>
    </label>
  </header>
  <div id="log" role="log" aria-live="polite" aria-label="Conversation"></div>
  <form id="form">
    <label class="sr-only" for="input">Message</label>
    <textarea id="input" rows="1" placeholder="Message… (Enter to send, Shift+Enter for newline)" autofocus></textarea>
    <button type="submit" id="send">Send</button>
  </form>
<script>
  // Conversation id is kept client-side for this browser session and echoed to
  // /chat/stream on every turn so history is maintained server-side by the Responses API.
  let conversationId = null;
  const log = document.getElementById('log');
  const form = document.getElementById('form');
  const input = document.getElementById('input');
  const send = document.getElementById('send');
  const modelSel = document.getElementById('model');

  function scroll() { log.scrollTop = log.scrollHeight; }

  function autogrow() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 160) + 'px';
  }

  function addRow(cls, meta) {
    const row = document.createElement('div');
    row.className = 'row ' + cls;
    if (meta) {
      const m = document.createElement('div');
      m.className = 'meta';
      m.textContent = meta;
      row.appendChild(m);
    }
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    row.appendChild(bubble);
    log.appendChild(row);
    scroll();
    return bubble;
  }

  function setSending(on) { send.disabled = on; input.disabled = on; }

  input.addEventListener('input', autogrow);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const message = input.value.trim();
    if (!message) return;
    const model = modelSel.value;

    addRow('user').textContent = message;
    input.value = ''; autogrow();
    setSending(true);

    // Bot bubble starts as a "typing…" indicator until the first token lands.
    const bubble = addRow('bot', model);
    bubble.innerHTML = '<span class="typing" aria-label="Assistant is typing"><span></span><span></span><span></span></span>';
    const textSpan = document.createElement('span');
    const caret = document.createElement('span');
    caret.className = 'caret';
    let started = false, acc = '';

    try {
      const res = await fetch('/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model, message, conversation_id: conversationId })
      });
      if (!res.ok) {
        let detail;
        try { detail = (await res.json()).detail; } catch (_) {}
        throw new Error(detail || res.statusText);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf('\\n\\n')) >= 0) {
          const line = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 2);
          if (!line.startsWith('data:')) continue;
          const data = JSON.parse(line.slice(5).trim());
          if (data.conversation_id) conversationId = data.conversation_id;
          if (data.error) throw new Error(data.error);
          if (typeof data.delta === 'string') {
            if (!started) {
              started = true;
              bubble.textContent = '';
              bubble.appendChild(textSpan);
              bubble.appendChild(caret);
            }
            acc += data.delta;
            textSpan.textContent = acc;
            scroll();
          }
        }
      }
      caret.remove();
      if (!started) { bubble.textContent = '(no response)'; }
    } catch (err) {
      caret.remove();
      bubble.classList.add('error');
      bubble.textContent = '⚠ ' + err.message;
    } finally {
      setSending(false);
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
