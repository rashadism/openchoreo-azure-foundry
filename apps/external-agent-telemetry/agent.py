"""External web-search agent hosted on OpenChoreo, observed by Azure AI Foundry.

Flow: this service emits OpenTelemetry spans that follow the GenAI semantic
conventions to the Application Insights resource connected to a Foundry project.
Foundry matches spans by `gen_ai.agent.id` and shows them in its Traces view once
the agent is registered in the portal (a one-time manual step, see README).

The agent answers questions by (1) running a keyless DuckDuckGo web search, then
(2) asking the Foundry-hosted model to answer grounded in those results, citing
sources by URL. A small browser UI is served at `/`.

Three auth surfaces (see README):
  - client -> this agent : your own concern (not handled here; add your gateway/authz)
  - agent  -> telemetry  : Application Insights connection string (NOT Entra)
  - agent  -> model      : Entra / service principal via DefaultAzureCredential
"""
import os
import time
import logging

import openai
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from opentelemetry import trace

# --- Web search tool (keyless DuckDuckGo) ---------------------------------------
# The maintained package is `ddgs`; older installs expose it as `duckduckgo_search`.
try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover - fallback for older package name
    from duckduckgo_search import DDGS

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


def web_search(query):
    """Run a keyless DuckDuckGo text search.

    Returns a list of {"title", "url", "snippet"} dicts (up to 4). Never raises:
    on any failure it logs and returns an empty list so a run still completes.
    """
    try:
        raw = DDGS().text(query, max_results=4) or []
    except Exception as exc:  # noqa: BLE001 - degrade gracefully, never crash a turn
        log.warning("web_search failed for %r: %s", query, exc)
        return []
    results = []
    for r in raw:
        results.append(
            {
                "title": r.get("title", "") or "",
                "url": r.get("href", "") or "",
                "snippet": r.get("body", "") or "",
            }
        )
    return results


app = FastAPI(title="OpenChoreo -> Foundry Web Search Agent")


class ChatRequest(BaseModel):
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


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(content=INDEX_HTML)


# Transient Foundry errors worth retrying the model call on.
_RETRYABLE_MODEL_ERRORS = (
    openai.InternalServerError,
    openai.APITimeoutError,
    openai.APIConnectionError,
)
# Fixed backoffs (seconds) applied after attempt 1 and attempt 2. No randomness.
_MODEL_RETRY_BACKOFFS = (0.5, 1.5)


def _create_response_with_retry(client, model, model_input):
    """Call the Foundry Responses API, retrying transient failures.

    Up to 3 attempts, catching InternalServerError (HTTP 500) plus timeout and
    connection errors from the OpenAI-compatible client. Sleeps a short fixed
    backoff (0.5s, then 1.5s) between attempts and re-raises the last exception
    if every attempt fails. Runs inside the caller's existing `chat` span.
    """
    last_exc = None
    for attempt in range(3):
        try:
            return client.responses.create(model=model, input=model_input)
        except _RETRYABLE_MODEL_ERRORS as exc:  # transient: retry a few times
            last_exc = exc
            log.warning("model call attempt %d/3 failed: %s", attempt + 1, exc)
            if attempt < len(_MODEL_RETRY_BACKOFFS):
                time.sleep(_MODEL_RETRY_BACKOFFS[attempt])
    raise last_exc


def _build_model_input(question, results):
    """Compose the model prompt: the question plus formatted search context."""
    if results:
        lines = ["Web search results:"]
        for i, r in enumerate(results, 1):
            lines.append(f"[{i}] {r['title']}")
            lines.append(f"    URL: {r['url']}")
            lines.append(f"    {r['snippet']}")
        context = "\n".join(lines)
    else:
        context = "Web search returned no results."
    return (
        "You are a helpful web-search assistant. Answer the user's question using "
        "the web search results below. Ground your answer in the results and cite "
        "sources inline by their URL (use markdown links). If the results do not "
        "contain the answer, say so plainly.\n\n"
        f"Question: {question}\n\n"
        f"{context}"
    )


@app.post("/chat")
def chat(body: ChatRequest):
    """Run one web-search agent turn and emit GenAI spans for Foundry.

    Span structure:
      invoke_agent
        -> execute_tool web_search   (the DuckDuckGo search)
        -> chat {MODEL_DEPLOYMENT}    (the grounded model call)
    """
    question = body.message
    with tracer.start_as_current_span("invoke_agent") as agent_span:
        agent_span.set_attribute("gen_ai.operation.name", "invoke_agent")
        agent_span.set_attribute("gen_ai.system", GEN_AI_SYSTEM)
        agent_span.set_attribute("gen_ai.agent.id", AGENT_ID)
        agent_span.set_attribute("gen_ai.agent.name", AGENT_NAME)

        # Tool span: the web search.
        with tracer.start_as_current_span("execute_tool web_search") as tool_span:
            tool_span.set_attribute("gen_ai.operation.name", "execute_tool")
            tool_span.set_attribute("gen_ai.system", GEN_AI_SYSTEM)
            tool_span.set_attribute("gen_ai.tool.name", "web_search")
            tool_span.set_attribute("gen_ai.agent.id", AGENT_ID)
            tool_span.set_attribute("gen_ai.tool.call.query", question)
            results = web_search(question)
            tool_span.set_attribute("gen_ai.tool.call.result_count", len(results))

        # Model span: the grounded inference call.
        with tracer.start_as_current_span(f"chat {MODEL_DEPLOYMENT}") as model_span:
            model_span.set_attribute("gen_ai.operation.name", "chat")
            model_span.set_attribute("gen_ai.system", GEN_AI_SYSTEM)
            model_span.set_attribute("gen_ai.request.model", MODEL_DEPLOYMENT)
            model_span.set_attribute("gen_ai.agent.id", AGENT_ID)

            client = get_openai_client()
            response = _create_response_with_retry(
                client, MODEL_DEPLOYMENT, _build_model_input(question, results)
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

    sources = [{"title": r["title"] or r["url"], "url": r["url"]} for r in results if r["url"]]
    return {"reply": text, "sources": sources, "agent_id": AGENT_ID}


# --- Browser UI (single self-contained page, light theme, no CDNs) ---------------
# Visual design is deliberately a sibling of the agent-mcp-chat "Agentic Chatbot"
# page: same palette / CSS variables, header (status dot + titlewrap), message
# bubbles, fonts, spacing, input row, and the SAME md() markdown renderer. This
# app has no MCP servers, so there is no Tools drawer; instead each answer renders
# a "Sources" section built from the /chat response's sources[].
INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Web Search Agent</title>
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

  /* two-phase loading indicator */
  .phase { display: inline-flex; align-items: center; gap: 8px; color: var(--muted); font-size: 14px; }
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

  /* Sources block under an answer (no MCP tools drawer in this app) */
  .sources { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--border); }
  .sources h4 {
    margin: 0 0 6px; font-size: 11px; text-transform: uppercase;
    letter-spacing: .06em; color: var(--muted); font-weight: 600;
  }
  .sources ol { margin: 0; padding-left: 18px; }
  .sources li { margin: 2px 0; }
  .sources a { color: var(--muted); text-decoration: none; font-size: 12.5px; }
  .sources a:hover { color: var(--accent); text-decoration: underline; }

  /* rendered markdown inside bot bubbles */
  .bot .msg.rendered { white-space: normal; }
  .bot .msg.rendered > :first-child { margin-top: 0; }
  .bot .msg p { margin: 0 0 8px; }
  .bot .msg p:last-child { margin-bottom: 0; }
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
    <span class="title">Web Search Agent</span>
    <span class="subtitle">Self-hosted on OpenChoreo &middot; observed by Azure AI Foundry</span>
  </span>
</header>
<div id="scroll">
  <div id="log">
    <div class="meta">Ask a question to get started.</div>
  </div>
</div>
<footer>
  <form id="f">
    <textarea id="m" rows="1" autocomplete="off"
      placeholder="Ask anything…"></textarea>
    <button id="b" type="submit">Send</button>
  </form>
</footer>
<script>
let busy = false;
const scroller = document.getElementById('scroll');
const log = document.getElementById('log');
const form = document.getElementById('f');
const input = document.getElementById('m');
const btn = document.getElementById('b');

function mdEscape(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function md(src){
  const blocks=[];
  src=src.replace(/```(\w*)\n?([\s\S]*?)```/g,(m,l,code)=>{blocks.push('<pre><code>'+mdEscape(code.replace(/\n$/,''))+'</code></pre>');return 'ZZCBZZ'+(blocks.length-1)+'ZZ';});
  src=mdEscape(src);
  src=src.replace(/`([^`\n]+)`/g,'<code>$1</code>');
  src=src.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  src=src.replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<em>$2</em>');
  src=src.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  const lines=src.split('\n');const out=[];let list=null;
  const closeList=()=>{if(list){out.push('</'+list+'>');list=null;}};
  for(const line of lines){let m;
    if(/^ZZCBZZ\d+ZZ$/.test(line.trim())){closeList();out.push(line.trim());continue;}
    if(m=line.match(/^(#{1,3})\s+(.*)$/)){closeList();out.push('<h'+m[1].length+'>'+m[2]+'</h'+m[1].length+'>');continue;}
    if(m=line.match(/^\s*[-*]\s+(.*)$/)){if(list!=='ul'){closeList();out.push('<ul>');list='ul';}out.push('<li>'+m[1]+'</li>');continue;}
    if(m=line.match(/^\s*\d+\.\s+(.*)$/)){if(list!=='ol'){closeList();out.push('<ol>');list='ol';}out.push('<li>'+m[1]+'</li>');continue;}
    closeList();if(line.trim()==='')continue;out.push('<p>'+line+'</p>');}
  closeList();
  let html=out.join('\n');html=html.replace(/ZZCBZZ(\d+)ZZ/g,(m,i)=>blocks[i]);
  return html;
}

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

// Two-phase loading: "Searching the web…" then "Thinking…" after ~1.2s. It is a
// single non-streaming request, so we approximate the phases with a timer.
function startLoading(bot) {
  bot.className = 'msg';
  bot.textContent = '';
  const phase = document.createElement('span');
  phase.className = 'phase';
  const typing = document.createElement('span');
  typing.className = 'typing';
  typing.innerHTML = '<span></span><span></span><span></span>';
  const label = document.createElement('span');
  label.textContent = '🔎 Searching the web…';
  phase.appendChild(typing);
  phase.appendChild(label);
  bot.appendChild(phase);
  const timer = setTimeout(() => { label.textContent = '💭 Thinking…'; }, 1200);
  return timer;
}

function renderAnswer(bot, reply, sources) {
  bot.className = 'msg rendered';
  bot.innerHTML = md(reply || '');
  if (Array.isArray(sources) && sources.length) {
    const box = document.createElement('div');
    box.className = 'sources';
    const h = document.createElement('h4');
    h.appendChild(document.createTextNode('Sources'));
    box.appendChild(h);
    const ol = document.createElement('ol');
    for (const s of sources) {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.href = s.url;
      a.target = '_blank';
      a.rel = 'noopener';
      a.appendChild(document.createTextNode(s.title || s.url));
      li.appendChild(a);
      ol.appendChild(li);
    }
    box.appendChild(ol);
    bot.appendChild(box);
  }
  scrollDown(false);
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
  const timer = startLoading(bot);

  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg})
    });
    clearTimeout(timer);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    renderAnswer(bot, data.reply, data.sources);
  } catch (err) {
    clearTimeout(timer);
    bot.className = 'msg error';
    bot.textContent = 'Something went wrong: ' + (err && err.message ? err.message : err);
    scrollDown(false);
  } finally {
    setBusy(false);
    input.focus();
  }
});
</script>
</body></html>"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
