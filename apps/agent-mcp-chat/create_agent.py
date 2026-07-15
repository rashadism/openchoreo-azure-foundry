#!/usr/bin/env python3
"""Create (or idempotently ensure) a Foundry *prompt agent* that has a public,
tokenless MCP tool attached.

Foundry agents are Entra-only (no API key). We authenticate with
azure-identity DefaultAzureCredential and request a token for the
`https://ai.azure.com/.default` scope, then POST the agent definition to the
Foundry project endpoint.

REST shape (Foundry projects "new" API, api-version=v1):

    POST {FOUNDRY_PROJECT_ENDPOINT}/agents?api-version=v1
    Authorization: Bearer <entra-token>
    {
      "name": "<AGENT_NAME>",
      "definition": {
        "kind": "prompt",
        "model": "<deployment>",
        "instructions": "...",
        "tools": [
          {
            "type": "mcp",
            "server_label": "api-specs",
            "server_url": "https://gitmcp.io/Azure/azure-rest-api-specs",
            "require_approval": "never"
          }
        ]
      }
    }

Idempotent: a 409 (agent already exists) is treated as success. Creating the
agent again with the same name mints a new *version*; re-running is safe.

Env:
    FOUNDRY_PROJECT_ENDPOINT  e.g. https://rashad-4421-resource.services.ai.azure.com/api/projects/rashad-4421
    AGENT_NAME                default "mcp-agent"
    FOUNDRY_MODEL_DEPLOYMENT  default "gpt-5-mini"
    MCP_SERVER_LABEL          default "api-specs"
    MCP_SERVER_URL            default "https://gitmcp.io/Azure/azure-rest-api-specs"
    # Service-principal creds picked up automatically by DefaultAzureCredential:
    AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_CLIENT_SECRET
    # ...or run `az login` locally.
"""
import json
import os
import sys

import requests
from azure.identity import DefaultAzureCredential

API_VERSION = "v1"
ENTRA_SCOPE = "https://ai.azure.com/.default"

ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
AGENT_NAME = os.environ.get("AGENT_NAME", "mcp-agent")
MODEL = os.environ.get("FOUNDRY_MODEL_DEPLOYMENT", "gpt-5-mini")
MCP_SERVER_LABEL = os.environ.get("MCP_SERVER_LABEL", "api-specs")
MCP_SERVER_URL = os.environ.get(
    "MCP_SERVER_URL", "https://gitmcp.io/Azure/azure-rest-api-specs"
)

INSTRUCTIONS = (
    "You are a helpful assistant that can call the attached MCP tools to answer "
    "questions about the Azure REST API specifications. When a question is about "
    "Azure REST APIs, resource schemas, or api-versions, use the '"
    + MCP_SERVER_LABEL
    + "' MCP tools to look up authoritative answers, then summarize clearly and "
    "cite the file or path you used."
)


def bearer_token() -> str:
    cred = DefaultAzureCredential()
    return cred.get_token(ENTRA_SCOPE).token


def main() -> int:
    body = {
        "name": AGENT_NAME,
        "description": "Prompt agent with a public tokenless MCP tool (gitmcp.io).",
        "definition": {
            "kind": "prompt",
            "model": MODEL,
            "instructions": INSTRUCTIONS,
            "tools": [
                {
                    "type": "mcp",
                    "server_label": MCP_SERVER_LABEL,
                    "server_url": MCP_SERVER_URL,
                    # "never" => agent auto-approves tool calls, so there is no
                    # interactive mcp_approval_request/response handshake to manage.
                    "require_approval": "never",
                }
            ],
        },
    }

    url = f"{ENDPOINT}/agents?api-version={API_VERSION}"
    headers = {
        "Authorization": f"Bearer {bearer_token()}",
        "Content-Type": "application/json",
    }

    print(f"POST {url}")
    print(f"  agent={AGENT_NAME} model={MODEL} mcp={MCP_SERVER_URL}")
    resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=60)

    if resp.status_code in (200, 201):
        data = resp.json()
        print(
            f"OK: created agent name={data.get('name')} "
            f"id={data.get('id')} version={data.get('version')}"
        )
        return 0
    if resp.status_code == 409:
        print("OK (idempotent): agent already exists (HTTP 409). Nothing to do.")
        return 0

    print(f"ERROR {resp.status_code}: {resp.text}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
