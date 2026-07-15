#!/usr/bin/env bash
# Create (idempotently) a Foundry prompt agent with a public tokenless MCP tool.
#
# Foundry agents are Entra-only. Get a token for scope https://ai.azure.com/.default.
#   - Local dev:   az account get-access-token --scope https://ai.azure.com/.default
#   - Service principal: use azure-identity (see create_agent.py) or `az login --service-principal`.
#
# Treats HTTP 409 (already exists) as success.
set -euo pipefail

: "${FOUNDRY_PROJECT_ENDPOINT:?set FOUNDRY_PROJECT_ENDPOINT}"
AGENT_NAME="${AGENT_NAME:-mcp-agent}"
FOUNDRY_MODEL_DEPLOYMENT="${FOUNDRY_MODEL_DEPLOYMENT:-gpt-5-mini}"
MCP_SERVER_LABEL="${MCP_SERVER_LABEL:-api-specs}"
MCP_SERVER_URL="${MCP_SERVER_URL:-https://gitmcp.io/Azure/azure-rest-api-specs}"

# Get an Entra bearer token (falls back to az CLI if AGENT_TOKEN not preset).
AGENT_TOKEN="${AGENT_TOKEN:-$(az account get-access-token --scope https://ai.azure.com/.default --query accessToken -o tsv)}"

ENDPOINT="${FOUNDRY_PROJECT_ENDPOINT%/}"

http_code=$(curl -sS -o /tmp/agent_create.out -w '%{http_code}' \
  -X POST "${ENDPOINT}/agents?api-version=v1" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${AGENT_TOKEN}" \
  -d @- <<JSON
{
  "name": "${AGENT_NAME}",
  "description": "Prompt agent with a public tokenless MCP tool (gitmcp.io).",
  "definition": {
    "kind": "prompt",
    "model": "${FOUNDRY_MODEL_DEPLOYMENT}",
    "instructions": "You are a helpful assistant that uses the '${MCP_SERVER_LABEL}' MCP tools to answer questions about the Azure REST API specifications.",
    "tools": [
      {
        "type": "mcp",
        "server_label": "${MCP_SERVER_LABEL}",
        "server_url": "${MCP_SERVER_URL}",
        "require_approval": "never"
      }
    ]
  }
}
JSON
)

if [[ "$http_code" == "200" || "$http_code" == "201" ]]; then
  echo "OK: agent created."
  cat /tmp/agent_create.out
elif [[ "$http_code" == "409" ]]; then
  echo "OK (idempotent): agent already exists (HTTP 409)."
else
  echo "ERROR HTTP ${http_code}:" >&2
  cat /tmp/agent_create.out >&2
  exit 1
fi
