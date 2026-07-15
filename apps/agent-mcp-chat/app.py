"""FastAPI chat UI for a Foundry prompt agent that has a public tokenless MCP tool.

Flow per turn:
  1. Ensure we have a valid Entra token (DefaultAzureCredential, scope
     https://ai.azure.com/.default). Foundry agents are Entra-only. A pre-issued
     token (e.g. an `az` token) can be supplied via AZURE_AI_TOKEN.
  2. If the browser has no conversation_id yet, create a Foundry conversation:
     POST {endpoint}/openai/v1/conversations  -> {"id": "conv_..."}
  3. Call the Responses API with an agent_reference + the conversation id:
     POST {endpoint}/openai/v1/responses
       {"agent_reference": {"type":"agent_reference","name": AGENT_NAME},
        "conversation": conv_id,
        "input": [{"role":"user","content": message}],
        "stream": true}
     The conversation object holds history server-side, so we only send the new
     turn each time and pass the same conversation id back and forth.

Because the MCP tool was created with require_approval="never", the agent
auto-approves tool calls: there is no interactive mcp_approval_request handshake
for this UI to handle. The reply is streamed as response.output_text.delta events
(non-streaming /chat reads it from response.output_text as a fallback).
"""
import json
import os
import threading
import time

import requests
from azure.identity import DefaultAzureCredential
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
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


def _responses_payload(message: str, conv_id: str, stream: bool) -> dict:
    return {
        "agent_reference": {"type": "agent_reference", "name": AGENT_NAME},
        "conversation": conv_id,
        "input": [{"role": "user", "content": message}],
        "stream": stream,
    }


def _sse(event: dict) -> str:
    """Serialise one event as an SSE `data:` frame for the browser."""
    return f"data: {json.dumps(event)}\n\n"


class ChatIn(BaseModel):
    message: str
    conversation_id: str | None = None


@app.get("/healthz")
def healthz():
    return {"status": "ok", "agent": AGENT_NAME, "endpoint_set": bool(ENDPOINT)}


@app.post("/chat")
def chat(body: ChatIn):
    """Non-streaming reply (kept for API compatibility / debugging)."""
    if not ENDPOINT:
        return JSONResponse(
            {"error": "FOUNDRY_PROJECT_ENDPOINT is not set"}, status_code=500
        )
    try:
        conv_id = body.conversation_id or _create_conversation()
        r = requests.post(
            f"{ENDPOINT}/openai/v1/responses",
            headers=_headers(),
            json=_responses_payload(body.message, conv_id, stream=False),
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        return {"reply": _extract_text(r.json()), "conversation_id": conv_id}
    except requests.HTTPError as e:
        detail = e.response.text if e.response is not None else str(e)
        return JSONResponse({"error": detail}, status_code=502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


def _stream_events(message: str, conversation_id: str | None):
    """Yield browser-facing SSE frames while consuming the upstream SSE stream.

    Emits: {"type":"conversation","conversation_id":...} once known,
           {"type":"delta","text":...} per output-text delta,
           {"type":"error","error":...} on failure,
           {"type":"done"} at the end.
    """
    if not ENDPOINT:
        yield _sse({"type": "error", "error": "FOUNDRY_PROJECT_ENDPOINT is not set"})
        yield _sse({"type": "done"})
        return
    try:
        conv_id = conversation_id or _create_conversation()
        # Hand the conversation id back immediately so the client can reuse it
        # even if the answer errors out mid-stream.
        yield _sse({"type": "conversation", "conversation_id": conv_id})

        with requests.post(
            f"{ENDPOINT}/openai/v1/responses",
            headers={**_headers(), "Accept": "text/event-stream"},
            json=_responses_payload(message, conv_id, stream=True),
            timeout=HTTP_TIMEOUT,
            stream=True,
        ) as r:
            if r.status_code >= 400:
                r.raise_for_status()
            got_text = False
            for raw in r.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data:"):
                    continue
                data = raw[len("data:"):].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    evt = json.loads(data)
                except json.JSONDecodeError:
                    continue
                etype = evt.get("type", "")
                if etype == "response.output_text.delta":
                    delta = evt.get("delta")
                    if delta:
                        got_text = True
                        yield _sse({"type": "delta", "text": delta})
                elif etype == "response.error" or etype == "error":
                    err = evt.get("error") or evt.get("message") or "stream error"
                    if isinstance(err, dict):
                        err = err.get("message", str(err))
                    yield _sse({"type": "error", "error": str(err)})
                elif etype == "response.completed" and not got_text:
                    # Fallback: pull text from the final response object.
                    resp = evt.get("response") or {}
                    text = _extract_text(resp)
                    if text:
                        got_text = True
                        yield _sse({"type": "delta", "text": text})
        yield _sse({"type": "done"})
    except requests.HTTPError as e:
        detail = e.response.text if e.response is not None else str(e)
        yield _sse({"type": "error", "error": detail})
        yield _sse({"type": "done"})
    except Exception as e:  # noqa: BLE001
        yield _sse({"type": "error", "error": str(e)})
        yield _sse({"type": "done"})


@app.post("/chat/stream")
def chat_stream(body: ChatIn):
    return StreamingResponse(
        _stream_events(body.message, body.conversation_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Foundry Agent + MCP chat</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #0f172a; --panel: #1e293b; --panel-2: #172033;
    --border: #29374d; --text: #e2e8f0; --muted: #94a3b8;
    --user: #2563eb; --bot: #263449; --accent: #3b82f6;
    --danger-bg: #3f1d24; --danger-border: #7f1d1d; --danger-text: #fecaca;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f6f7f9; --panel: #ffffff; --panel-2: #ffffff;
      --border: #e2e8f0; --text: #0f172a; --muted: #64748b;
      --user: #2563eb; --bot: #f1f5f9; --accent: #2563eb;
      --danger-bg: #fef2f2; --danger-border: #fecaca; --danger-text: #b91c1c;
    }
    .bot { color: var(--text); }
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    margin: 0; background: var(--bg); color: var(--text);
    display: flex; flex-direction: column; height: 100vh;
  }
  header {
    padding: 12px 18px; background: var(--panel);
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 10px; flex: 0 0 auto;
  }
  header .dot { width: 8px; height: 8px; border-radius: 50%; background: #22c55e; flex: 0 0 auto; }
  header .title { font-weight: 600; font-size: 15px; }
  header .tool {
    margin-left: auto; font-size: 12px; color: var(--muted);
    border: 1px solid var(--border); border-radius: 999px; padding: 4px 10px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 55%;
  }
  header .tool code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  #scroll { flex: 1 1 auto; overflow-y: auto; }
  #log {
    max-width: 760px; margin: 0 auto; padding: 20px 18px;
    display: flex; flex-direction: column; gap: 12px;
  }
  .row { display: flex; }
  .row.user { justify-content: flex-end; }
  .row.bot { justify-content: flex-start; }
  .msg {
    padding: 10px 14px; border-radius: 14px; white-space: pre-wrap;
    line-height: 1.5; font-size: 14.5px; max-width: 82%;
    word-wrap: break-word; overflow-wrap: anywhere;
  }
  .user .msg { background: var(--user); color: #fff; border-bottom-right-radius: 4px; }
  .bot .msg  { background: var(--bot); border: 1px solid var(--border); border-bottom-left-radius: 4px; }
  .bot .msg.error { background: var(--danger-bg); border-color: var(--danger-border); color: var(--danger-text); }
  .meta { font-size: 12.5px; color: var(--muted); text-align: center; padding: 4px 0; }
  .caret {
    display: inline-block; width: 7px; height: 1.05em; margin-left: 1px;
    background: var(--accent); border-radius: 1px; vertical-align: text-bottom;
    animation: blink 1s steps(2, start) infinite;
  }
  @keyframes blink { 50% { opacity: 0; } }
  .typing { display: inline-flex; gap: 4px; align-items: center; }
  .typing span {
    width: 6px; height: 6px; border-radius: 50%; background: var(--muted);
    animation: bounce 1.2s infinite ease-in-out both;
  }
  .typing span:nth-child(2) { animation-delay: .15s; }
  .typing span:nth-child(3) { animation-delay: .3s; }
  @keyframes bounce { 0%, 80%, 100% { opacity: .3; } 40% { opacity: 1; } }
  footer { flex: 0 0 auto; border-top: 1px solid var(--border); background: var(--panel); }
  form {
    display: flex; gap: 8px; align-items: flex-end;
    max-width: 760px; margin: 0 auto; padding: 12px 18px;
  }
  textarea {
    flex: 1; resize: none; padding: 11px 12px; border-radius: 12px;
    border: 1px solid var(--border); background: var(--panel-2); color: var(--text);
    font: inherit; font-size: 14.5px; line-height: 1.4; max-height: 160px; overflow-y: auto;
  }
  textarea:focus { outline: none; border-color: var(--accent); }
  button {
    padding: 11px 18px; border: 0; border-radius: 12px; background: var(--accent);
    color: #fff; font-size: 14.5px; font-weight: 500; cursor: pointer; flex: 0 0 auto;
  }
  button:disabled { opacity: .5; cursor: default; }
</style></head>
<body>
<header>
  <span class="dot"></span>
  <span class="title">Foundry Agent + MCP</span>
  <span class="tool">tool: <code>gitmcp.io/Azure/azure-rest-api-specs</code></span>
</header>
<div id="scroll">
  <div id="log">
    <div class="meta">Ask about Azure REST API specs. The agent calls the MCP tool automatically.</div>
  </div>
</div>
<footer>
  <form id="f">
    <textarea id="m" rows="1" autocomplete="off"
      placeholder="Ask about Azure REST API specs…  (Enter to send, Shift+Enter for newline)"></textarea>
    <button id="b" type="submit">Send</button>
  </form>
</footer>
<script>
let conversationId = null;
let busy = false;
const scroller = document.getElementById('scroll');
const log = document.getElementById('log');
const form = document.getElementById('f');
const input = document.getElementById('m');
const btn = document.getElementById('b');

function atBottom() {
  return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
}
function scrollDown(force) {
  if (force || atBottom()) scroller.scrollTop = scroller.scrollHeight;
}

function addRow(cls) {
  const row = document.createElement('div');
  row.className = 'row ' + cls;
  const msg = document.createElement('div');
  msg.className = 'msg';
  row.appendChild(msg);
  log.appendChild(row);
  scrollDown(true);
  return msg;
}

function autosize() {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 160) + 'px';
}
input.addEventListener('input', autosize);

input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

function setBusy(v) {
  busy = v;
  btn.disabled = v;
  input.disabled = v;
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  if (busy) return;
  const msg = input.value.trim();
  if (!msg) return;

  addRow('user').textContent = msg;
  input.value = '';
  autosize();
  setBusy(true);

  const bot = addRow('bot');
  bot.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';
  let text = '';
  let started = false;

  function render(streaming) {
    bot.textContent = text;
    if (streaming) {
      const caret = document.createElement('span');
      caret.className = 'caret';
      bot.appendChild(caret);
    }
    scrollDown(false);
  }

  try {
    const res = await fetch('/chat/stream', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg, conversation_id: conversationId})
    });
    if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let errored = false;

    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {stream: true});
      const frames = buf.split('\\n\\n');
      buf = frames.pop();
      for (const frame of frames) {
        const line = frame.split('\\n').find(l => l.startsWith('data:'));
        if (!line) continue;
        let evt;
        try { evt = JSON.parse(line.slice(5).trim()); } catch (_) { continue; }
        if (evt.type === 'conversation') {
          conversationId = evt.conversation_id;
        } else if (evt.type === 'delta') {
          if (!started) { started = true; bot.textContent = ''; }
          text += evt.text;
          render(true);
        } else if (evt.type === 'error') {
          errored = true;
          bot.className = 'msg error';
          bot.textContent = 'Error: ' + evt.error;
          scrollDown(false);
        } else if (evt.type === 'done') {
          if (!errored) render(false);
        }
      }
    }
    if (!started && !errored) {
      bot.textContent = '(no response)';
    }
  } catch (err) {
    bot.className = 'msg error';
    bot.textContent = 'Error: ' + err;
  } finally {
    setBusy(false);
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
