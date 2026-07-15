"""FastAPI chat UI for a Foundry prompt agent that has a public tokenless MCP tool.

Flow per turn:
  1. Ensure we have a valid Entra token (DefaultAzureCredential, scope
     https://ai.azure.com/.default). Foundry agents are Entra-only.
  2. If the browser has no conversation_id yet, create a Foundry conversation:
     POST {endpoint}/openai/v1/conversations  -> {"id": "conv_..."}
  3. Call the Responses API with an agent_reference + the conversation id:
     POST {endpoint}/openai/v1/responses
       {"agent_reference": {"type":"agent_reference","name": AGENT_NAME},
        "conversation": conv_id,
        "input": [{"role":"user","content": message}]}
     The conversation object holds history server-side, so we only send the new
     turn each time and pass the same conversation id back and forth.

Because the MCP tool was created with require_approval="never", the agent
auto-approves tool calls: there is no interactive mcp_approval_request handshake
for this UI to handle. The reply is read from response.output_text (or assembled
from the output items as a fallback).
"""
import os
import threading
import time

import requests
from azure.identity import DefaultAzureCredential
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

ENTRA_SCOPE = "https://ai.azure.com/.default"
ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
AGENT_NAME = os.environ.get("AGENT_NAME", "mcp-agent")
PORT = int(os.environ.get("PORT", "8080"))
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "120"))

app = FastAPI(title="agent-mcp-chat")

# --- Entra token cache (thread-safe, refreshed ~5 min before expiry) ---------
_cred = DefaultAzureCredential()
_tok_lock = threading.Lock()
_tok = {"value": None, "exp": 0.0}


def _token() -> str:
    # Interim path for testing before a service principal: use a pre-issued
    # token (e.g. an `az` token) when AZURE_AI_TOKEN is set.
    static = os.environ.get("AZURE_AI_TOKEN")
    if static:
        return static
    with _tok_lock:
        if not _tok["value"] or time.time() > _tok["exp"] - 300:
            t = _cred.get_token(ENTRA_SCOPE)
            _tok["value"], _tok["exp"] = t.token, float(t.expires_on)
        return _tok["value"]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
    }


def _create_conversation() -> str:
    r = requests.post(
        f"{ENDPOINT}/openai/v1/conversations",
        headers=_headers(),
        json={"items": []},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["id"]


def _extract_text(data: dict) -> str:
    # Prefer the convenience field; fall back to concatenating output text parts.
    if data.get("output_text"):
        return data["output_text"]
    parts = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") in ("output_text", "text") and c.get("text"):
                    parts.append(c["text"])
    return "\n".join(parts) if parts else "(no text in response)"


class ChatIn(BaseModel):
    message: str
    conversation_id: str | None = None


@app.get("/healthz")
def healthz():
    return {"status": "ok", "agent": AGENT_NAME, "endpoint_set": bool(ENDPOINT)}


@app.post("/chat")
def chat(body: ChatIn):
    if not ENDPOINT:
        return JSONResponse(
            {"error": "FOUNDRY_PROJECT_ENDPOINT is not set"}, status_code=500
        )
    try:
        conv_id = body.conversation_id or _create_conversation()
        payload = {
            "agent_reference": {"type": "agent_reference", "name": AGENT_NAME},
            "conversation": conv_id,
            "input": [{"role": "user", "content": body.message}],
        }
        r = requests.post(
            f"{ENDPOINT}/openai/v1/responses",
            headers=_headers(),
            json=payload,
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        return {"reply": _extract_text(r.json()), "conversation_id": conv_id}
    except requests.HTTPError as e:
        detail = e.response.text if e.response is not None else str(e)
        return JSONResponse({"error": detail}, status_code=502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Foundry Agent + MCP chat</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; margin: 0; background: #0f172a; color: #e2e8f0; }
  header { padding: 14px 18px; background: #1e293b; font-weight: 600; }
  header small { font-weight: 400; color: #94a3b8; }
  #log { max-width: 760px; margin: 0 auto; padding: 18px; display: flex; flex-direction: column; gap: 10px; }
  .msg { padding: 10px 14px; border-radius: 12px; white-space: pre-wrap; line-height: 1.4; }
  .user { background: #2563eb; align-self: flex-end; max-width: 80%; }
  .bot  { background: #334155; align-self: flex-start; max-width: 80%; }
  .meta { font-size: 12px; color: #94a3b8; align-self: center; }
  form { position: sticky; bottom: 0; display: flex; gap: 8px; max-width: 760px;
         margin: 0 auto; padding: 12px 18px; background: #0f172a; }
  input { flex: 1; padding: 12px; border-radius: 10px; border: 1px solid #334155;
          background: #1e293b; color: #e2e8f0; font-size: 15px; }
  button { padding: 12px 18px; border: 0; border-radius: 10px; background: #2563eb;
           color: white; font-size: 15px; cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; }
</style></head>
<body>
<header>Foundry Agent + MCP <small>&mdash; tool: gitmcp.io/Azure/azure-rest-api-specs (public, tokenless)</small></header>
<div id="log">
  <div class="meta">Ask about Azure REST API specs. The agent calls the MCP tool automatically.</div>
</div>
<form id="f">
  <input id="m" autocomplete="off" placeholder="e.g. What api-versions exist for Microsoft.Storage storageAccounts?" />
  <button id="b" type="submit">Send</button>
</form>
<script>
let conversationId = null;
const log = document.getElementById('log');
const form = document.getElementById('f');
const input = document.getElementById('m');
const btn = document.getElementById('b');

function add(text, cls) {
  const d = document.createElement('div');
  d.className = 'msg ' + cls;
  d.textContent = text;
  log.appendChild(d);
  d.scrollIntoView({behavior: 'smooth'});
  return d;
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const msg = input.value.trim();
  if (!msg) return;
  add(msg, 'user');
  input.value = '';
  btn.disabled = true;
  const thinking = add('...', 'bot');
  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg, conversation_id: conversationId})
    });
    const data = await res.json();
    if (data.error) { thinking.textContent = 'Error: ' + data.error; }
    else { thinking.textContent = data.reply; conversationId = data.conversation_id; }
  } catch (err) {
    thinking.textContent = 'Error: ' + err;
  } finally {
    btn.disabled = false;
    input.focus();
  }
});
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
