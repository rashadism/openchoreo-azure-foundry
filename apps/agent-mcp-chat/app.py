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

# --- Runtime-discovered MCP tools cache --------------------------------------
# Maps server_label -> [{"name", "description"}]. Populated when the agent emits
# an `mcp_list_tools` item during a streamed turn, so /tools can enrich the
# server list and later page loads see tools without a fresh discovery.
_mcp_tools_lock = threading.Lock()
_mcp_tools_cache: dict = {}


def _cache_mcp_list_tools(item: dict) -> tuple:
    """Cache tool names+descriptions from an mcp_list_tools item.

    Returns (server_label, [{name, description}, ...]) or (None, None).
    """
    label = item.get("server_label")
    raw = item.get("tools")
    if not label or not isinstance(raw, list):
        return None, None
    tools = []
    for t in raw:
        if isinstance(t, dict) and t.get("name"):
            tools.append(
                {"name": t.get("name"), "description": t.get("description") or ""}
            )
    with _mcp_tools_lock:
        _mcp_tools_cache[label] = tools
    return label, tools

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


def _extract_mcp_tools(agent: dict) -> list:
    """Pull the MCP servers out of an agent definition.

    The tools array can live under versions.latest.definition.tools or
    definition.tools depending on the shape returned. Returns a list of
    {server_label, server_url} for every tool with type == "mcp".
    """
    tools = None
    versions = agent.get("versions")
    if isinstance(versions, dict):
        latest = versions.get("latest")
        if isinstance(latest, dict):
            definition = latest.get("definition")
            if isinstance(definition, dict):
                tools = definition.get("tools")
    if tools is None:
        definition = agent.get("definition")
        if isinstance(definition, dict):
            tools = definition.get("tools")
    if tools is None:
        tools = agent.get("tools")
    out = []
    if isinstance(tools, list):
        for t in tools:
            if isinstance(t, dict) and t.get("type") == "mcp":
                out.append(
                    {
                        "server_label": t.get("server_label"),
                        "server_url": t.get("server_url"),
                    }
                )
    return out


@app.get("/tools")
def tools():
    """Return the agent's configured MCP servers as [{server_label, server_url}]."""
    if not ENDPOINT:
        return JSONResponse(
            {"error": "FOUNDRY_PROJECT_ENDPOINT is not set"}, status_code=500
        )
    try:
        r = requests.get(
            f"{ENDPOINT}/agents/{AGENT_NAME}?api-version=v1",
            headers=_headers(),
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        servers = _extract_mcp_tools(r.json())
        with _mcp_tools_lock:
            for s in servers:
                s["tools"] = list(_mcp_tools_cache.get(s.get("server_label"), []))
        return {"tools": servers}
    except requests.HTTPError as e:
        detail = e.response.text if e.response is not None else str(e)
        return JSONResponse({"error": detail}, status_code=502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


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
           {"type":"tool","server":...,"name":...,"status":...} per MCP call,
           {"type":"tools_listed","server":...,"tools":[{name,description}]} on discovery,
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
            # Track MCP calls by item id so a bare completed/done event can be
            # matched back to its server_label/name. `mcp_done` guards against
            # emitting a duplicate "done" when both completed + output_item.done
            # arrive for the same call.
            mcp_calls = {}
            mcp_done = set()
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
                elif etype == "response.output_item.added":
                    item = evt.get("item") or {}
                    if item.get("type") == "mcp_call":
                        info = {
                            "server": item.get("server_label"),
                            "name": item.get("name"),
                        }
                        iid = item.get("id")
                        if iid:
                            mcp_calls[iid] = info
                        yield _sse({"type": "tool", **info, "status": "running"})
                elif etype == "response.mcp_call.completed":
                    iid = evt.get("item_id")
                    if iid not in mcp_done:
                        mcp_done.add(iid)
                        info = mcp_calls.get(iid, {})
                        yield _sse(
                            {
                                "type": "tool",
                                "server": info.get("server"),
                                "name": info.get("name"),
                                "status": "done",
                            }
                        )
                elif etype == "response.output_item.done":
                    item = evt.get("item") or {}
                    if item.get("type") == "mcp_list_tools":
                        label, tools_ = _cache_mcp_list_tools(item)
                        if label is not None:
                            yield _sse(
                                {
                                    "type": "tools_listed",
                                    "server": label,
                                    "tools": tools_,
                                }
                            )
                    elif item.get("type") == "mcp_call":
                        iid = item.get("id")
                        if iid not in mcp_done:
                            mcp_done.add(iid)
                            info = mcp_calls.get(iid, {})
                            yield _sse(
                                {
                                    "type": "tool",
                                    "server": item.get("server_label")
                                    or info.get("server"),
                                    "name": item.get("name") or info.get("name"),
                                    "status": "done",
                                }
                            )
                elif etype == "response.mcp_list_tools.completed":
                    item = evt.get("item") or {}
                    if item.get("type") == "mcp_list_tools":
                        label, tools_ = _cache_mcp_list_tools(item)
                        if label is not None:
                            yield _sse(
                                {
                                    "type": "tools_listed",
                                    "server": label,
                                    "tools": tools_,
                                }
                            )
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
<title>Agentic Chatbot</title>
<style>
  :root {
    color-scheme: light;
    --bg: #f6f7f9; --panel: #ffffff; --panel-2: #ffffff;
    --border: #e2e8f0; --text: #0f172a; --muted: #64748b;
    --user: #2563eb; --bot: #f1f5f9; --accent: #2563eb;
    --danger-bg: #fef2f2; --danger-border: #fecaca; --danger-text: #b91c1c;
  }
  .bot { color: var(--text); }
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
  header .titlewrap { display: flex; flex-direction: column; line-height: 1.2; }
  header .title { font-weight: 600; font-size: 15px; }
  header .subtitle { font-size: 11.5px; color: var(--muted); }
  header .tools-btn {
    margin-left: auto; flex: 0 0 auto;
    font-size: 13px; font-weight: 500; color: var(--text);
    background: var(--panel); border: 1px solid var(--border); border-radius: 999px;
    padding: 6px 12px; cursor: pointer; display: inline-flex; align-items: center; gap: 5px;
    transition: background .15s, border-color .15s;
  }
  header .tools-btn:hover { background: #f1f5f9; border-color: #cbd5e1; }

  /* slide-in Tools drawer */
  .drawer-overlay {
    position: fixed; inset: 0; background: rgba(15, 23, 42, .28);
    opacity: 0; transition: opacity .2s ease; z-index: 20;
  }
  .drawer-overlay.open { opacity: 1; }
  .drawer {
    position: fixed; top: 0; right: 0; height: 100%; width: 360px; max-width: 88vw;
    background: var(--panel); border-left: 1px solid var(--border);
    box-shadow: -8px 0 24px rgba(15, 23, 42, .08);
    transform: translateX(100%); transition: transform .22s ease;
    z-index: 21; display: flex; flex-direction: column;
  }
  .drawer.open { transform: translateX(0); }
  .drawer-head {
    display: flex; align-items: center; gap: 10px;
    padding: 14px 16px; border-bottom: 1px solid var(--border); flex: 0 0 auto;
  }
  .drawer-title { font-weight: 600; font-size: 14px; }
  .drawer-close {
    margin-left: auto; background: transparent; border: 0; color: var(--muted);
    font-size: 22px; line-height: 1; cursor: pointer; padding: 0 4px; border-radius: 6px;
  }
  .drawer-close:hover { color: var(--text); }
  .drawer-body { flex: 1 1 auto; overflow-y: auto; padding: 14px 16px; display: flex; flex-direction: column; gap: 12px; }
  .drawer-empty { font-size: 13px; color: var(--muted); }
  .srv-card { border: 1px solid var(--border); border-radius: 12px; padding: 12px; background: var(--panel-2); }
  .srv-label { font-weight: 600; font-size: 13.5px; }
  .srv-url {
    display: block; margin-top: 2px; font-size: 11.5px; color: var(--muted);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    word-break: break-all; text-decoration: none;
  }
  .srv-url:hover { text-decoration: underline; color: var(--accent); }
  .srv-tools { list-style: none; margin: 10px 0 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
  .srv-tools li { display: flex; flex-direction: column; gap: 2px; }
  .tool-name {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: var(--accent);
  }
  .tool-desc { font-size: 12px; color: var(--text); line-height: 1.4; }
  .srv-none { margin: 10px 0 0; font-size: 12px; color: var(--muted); font-style: italic; }
  @media (max-width: 480px) { .drawer { width: 100%; max-width: 100%; } }
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

  /* assistant turn: tool chips stacked above the message bubble */
  .turn { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; max-width: 82%; }
  .bot .msg { max-width: 100%; }
  .tools { display: flex; flex-direction: column; gap: 4px; }
  .tool-chip {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11.5px; color: var(--muted);
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 999px; padding: 3px 9px;
    display: inline-flex; align-items: center; max-width: 100%;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .tool-chip .tc-server { color: var(--text); }
  .tool-chip .tc-name { color: var(--accent); }
  .tool-chip.running .tc-status { color: var(--muted); }
  .tool-chip.done { opacity: .85; }
  .tool-chip.done .tc-status { color: #16a34a; }

  /* rendered markdown inside bot bubbles */
  .bot .msg.rendered { white-space: normal; }
  .bot .msg.rendered > :first-child { margin-top: 0; }
  .bot .msg.rendered > :last-child { margin-bottom: 0; }
  .bot .msg p { margin: 0 0 8px; }
  .bot .msg strong { font-weight: 600; }
  .bot .msg em { font-style: italic; }
  .bot .msg code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .9em;
    background: #eef2f7; border: 1px solid var(--border); border-radius: 4px; padding: 1px 4px;
  }
  .bot .msg pre { margin: 8px 0; }
  .bot .msg pre code {
    display: block; padding: 10px 12px; border-radius: 8px;
    background: #f8fafc; border: 1px solid var(--border);
    overflow-x: auto; white-space: pre; font-size: .9em;
  }
  .bot .msg h1, .bot .msg h2, .bot .msg h3 { margin: 10px 0 6px; line-height: 1.3; font-weight: 600; }
  .bot .msg h1 { font-size: 1.25em; }
  .bot .msg h2 { font-size: 1.15em; }
  .bot .msg h3 { font-size: 1.05em; }
  .bot .msg ul, .bot .msg ol { margin: 6px 0; padding-left: 22px; }
  .bot .msg li { margin: 2px 0; }
  .bot .msg a { color: var(--accent); text-decoration: underline; }
</style></head>
<body>
<header>
  <span class="dot"></span>
  <span class="titlewrap">
    <span class="title">Agentic Chatbot</span>
    <span class="subtitle">Prompt agent deployed in Azure AI Foundry</span>
  </span>
  <button class="tools-btn" id="toolsBtn" type="button" aria-label="Show tools and MCP servers" aria-expanded="false">
    <span aria-hidden="true">🧰</span> Tools
  </button>
</header>
<div class="drawer-overlay" id="drawerOverlay" hidden></div>
<aside class="drawer" id="drawer" aria-hidden="true" aria-label="MCP servers and tools">
  <div class="drawer-head">
    <span class="drawer-title">MCP servers &amp; tools</span>
    <button class="drawer-close" id="drawerClose" type="button" aria-label="Close">&times;</button>
  </div>
  <div class="drawer-body" id="drawerBody"></div>
</aside>
<div id="scroll">
  <div id="log">
    <div class="meta">Ask a question to get started.</div>
  </div>
</div>
<footer>
  <form id="f">
    <textarea id="m" rows="1" autocomplete="off"
      placeholder="Message the agent…"></textarea>
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

function mdEscape(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function md(src){
  const blocks=[];
  src=src.replace(/```(\\w*)\\n?([\\s\\S]*?)```/g,(m,l,code)=>{blocks.push('<pre><code>'+mdEscape(code.replace(/\\n$/,''))+'</code></pre>');return 'ZZCBZZ'+(blocks.length-1)+'ZZ';});
  src=mdEscape(src);
  src=src.replace(/`([^`\\n]+)`/g,'<code>$1</code>');
  src=src.replace(/\\*\\*([^*]+)\\*\\*/g,'<strong>$1</strong>');
  src=src.replace(/(^|[^*])\\*([^*\\n]+)\\*/g,'$1<em>$2</em>');
  src=src.replace(/\\[([^\\]]+)\\]\\((https?:\\/\\/[^)\\s]+)\\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  const lines=src.split('\\n');const out=[];let list=null;
  const closeList=()=>{if(list){out.push('</'+list+'>');list=null;}};
  for(const line of lines){let m;
    if(/^ZZCBZZ\\d+ZZ$/.test(line.trim())){closeList();out.push(line.trim());continue;}
    if(m=line.match(/^(#{1,3})\\s+(.*)$/)){closeList();out.push('<h'+m[1].length+'>'+m[2]+'</h'+m[1].length+'>');continue;}
    if(m=line.match(/^\\s*[-*]\\s+(.*)$/)){if(list!=='ul'){closeList();out.push('<ul>');list='ul';}out.push('<li>'+m[1]+'</li>');continue;}
    if(m=line.match(/^\\s*\\d+\\.\\s+(.*)$/)){if(list!=='ol'){closeList();out.push('<ol>');list='ol';}out.push('<li>'+m[1]+'</li>');continue;}
    closeList();if(line.trim()==='')continue;out.push('<p>'+line+'</p>');}
  closeList();
  let html=out.join('\\n');html=html.replace(/ZZCBZZ(\\d+)ZZ/g,(m,i)=>blocks[i]);
  return html;
}

// --- Tools / MCP servers drawer ---------------------------------------------
const TOOLS_LS_KEY = 'mcpToolsCache';
const drawer = document.getElementById('drawer');
const drawerOverlay = document.getElementById('drawerOverlay');
const drawerBody = document.getElementById('drawerBody');
const toolsBtn = document.getElementById('toolsBtn');

let servers = [];                 // [{server_label, server_url}]
let toolsByServer = loadToolCache(); // { label: [{name, description}] }

function loadToolCache() {
  try {
    const raw = localStorage.getItem(TOOLS_LS_KEY);
    const obj = raw ? JSON.parse(raw) : {};
    return (obj && typeof obj === 'object') ? obj : {};
  } catch (_) { return {}; }
}
function saveToolCache() {
  try { localStorage.setItem(TOOLS_LS_KEY, JSON.stringify(toolsByServer)); } catch (_) {}
}

function renderDrawer() {
  drawerBody.textContent = '';
  if (!servers.length) {
    const empty = document.createElement('div');
    empty.className = 'drawer-empty';
    empty.textContent = 'No MCP servers configured.';
    drawerBody.appendChild(empty);
    return;
  }
  for (const s of servers) {
    const label = s.server_label || 'mcp';
    const card = document.createElement('div');
    card.className = 'srv-card';

    const name = document.createElement('div');
    name.className = 'srv-label';
    name.textContent = label;
    card.appendChild(name);

    if (s.server_url) {
      const url = document.createElement('a');
      url.className = 'srv-url';
      url.href = s.server_url;
      url.target = '_blank';
      url.rel = 'noopener';
      url.textContent = s.server_url;
      card.appendChild(url);
    }

    const tools = toolsByServer[label] || [];
    if (tools.length) {
      const ul = document.createElement('ul');
      ul.className = 'srv-tools';
      for (const t of tools) {
        const li = document.createElement('li');
        const tn = document.createElement('span');
        tn.className = 'tool-name';
        tn.textContent = t.name || 'tool';
        li.appendChild(tn);
        if (t.description) {
          const td = document.createElement('span');
          td.className = 'tool-desc';
          td.textContent = t.description;
          li.appendChild(td);
        }
        ul.appendChild(li);
      }
      card.appendChild(ul);
    } else {
      const none = document.createElement('div');
      none.className = 'srv-none';
      none.textContent = 'Tools appear after first use.';
      card.appendChild(none);
    }
    drawerBody.appendChild(card);
  }
}

function updateServerTools(label, tools) {
  if (!label || !Array.isArray(tools)) return;
  toolsByServer[label] = tools.map(t => ({
    name: t.name || 'tool',
    description: t.description || ''
  }));
  saveToolCache();
  renderDrawer();
}

async function loadTools() {
  renderDrawer(); // show cached state immediately
  try {
    const r = await fetch('/tools');
    if (!r.ok) return;
    const data = await r.json();
    const list = (data && Array.isArray(data.tools)) ? data.tools : [];
    servers = list.map(t => ({ server_label: t.server_label, server_url: t.server_url }));
    for (const t of list) {
      if (t.server_label && Array.isArray(t.tools) && t.tools.length) {
        toolsByServer[t.server_label] = t.tools.map(x => ({
          name: x.name || 'tool', description: x.description || ''
        }));
      }
    }
    saveToolCache();
    renderDrawer();
  } catch (_) { /* keep cached render on failure */ }
}

function openDrawer() {
  drawerOverlay.hidden = false;
  requestAnimationFrame(() => {
    drawerOverlay.classList.add('open');
    drawer.classList.add('open');
  });
  drawer.setAttribute('aria-hidden', 'false');
  toolsBtn.setAttribute('aria-expanded', 'true');
}
function closeDrawer() {
  drawerOverlay.classList.remove('open');
  drawer.classList.remove('open');
  drawer.setAttribute('aria-hidden', 'true');
  toolsBtn.setAttribute('aria-expanded', 'false');
  setTimeout(() => { drawerOverlay.hidden = true; }, 220);
}

toolsBtn.addEventListener('click', openDrawer);
drawerOverlay.addEventListener('click', closeDrawer);
document.getElementById('drawerClose').addEventListener('click', closeDrawer);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && drawer.classList.contains('open')) closeDrawer();
});

loadTools();

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

  // Assistant turn: a column holding tool-call chips above the message bubble.
  const botRow = document.createElement('div');
  botRow.className = 'row bot';
  const turn = document.createElement('div');
  turn.className = 'turn';
  const tools = document.createElement('div');
  tools.className = 'tools';
  const bot = document.createElement('div');
  bot.className = 'msg';
  turn.appendChild(tools);
  turn.appendChild(bot);
  botRow.appendChild(turn);
  log.appendChild(botRow);
  scrollDown(true);

  bot.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';
  let text = '';
  let started = false;
  const pendingTools = [];

  function toolLabel(chip, server, name, status) {
    chip.textContent = '';
    chip.appendChild(document.createTextNode('🔧 '));
    const s = document.createElement('span');
    s.className = 'tc-server';
    s.textContent = server || 'mcp';
    const sep = document.createElement('span');
    sep.textContent = ' · ';
    const n = document.createElement('span');
    n.className = 'tc-name';
    n.textContent = name || 'tool';
    const st = document.createElement('span');
    st.className = 'tc-status';
    st.textContent = ' — ' + (status === 'done' ? 'done' : 'running…');
    chip.append(s, sep, n, st);
  }

  function toolEvent(evt) {
    if (evt.status === 'running') {
      const chip = document.createElement('div');
      chip.className = 'tool-chip running';
      chip.dataset.key = (evt.server || '') + '|' + (evt.name || '');
      toolLabel(chip, evt.server, evt.name, 'running');
      tools.appendChild(chip);
      pendingTools.push(chip);
    } else if (evt.status === 'done') {
      const key = (evt.server || '') + '|' + (evt.name || '');
      let chip = null;
      for (let i = 0; i < pendingTools.length; i++) {
        if (pendingTools[i].dataset.key === key) { chip = pendingTools.splice(i, 1)[0]; break; }
      }
      if (!chip && pendingTools.length) chip = pendingTools.shift();
      if (chip) {
        chip.className = 'tool-chip done';
        const server = evt.server || (chip.dataset.key.split('|')[0]);
        const name = evt.name || (chip.dataset.key.split('|')[1]);
        toolLabel(chip, server, name, 'done');
      }
    }
    scrollDown(false);
  }

  function render(streaming) {
    bot.className = 'msg rendered';
    bot.innerHTML = md(text);
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
        } else if (evt.type === 'tool') {
          toolEvent(evt);
        } else if (evt.type === 'tools_listed') {
          updateServerTools(evt.server, evt.tools);
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
