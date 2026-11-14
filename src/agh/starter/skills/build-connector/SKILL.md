---
name: build-connector
description: "How to connect an API service or MCP server. Use before calling create_connector."
---

# Build a connector

Prefer an OpenAPI import: `create_connector(name, openapi="<url>", include="<regex of operation ids>")`.
Keep operations to what is needed; each one becomes a tool in the model's context.

REST config:

```yaml
name: example
type: rest
description: Example service
base_url: https://api.example.com/v1
auth: {type: bearer, token_env: EXAMPLE_TOKEN}
# or {type: api_key, in: header, name: X-API-Key, key_env: EXAMPLE_KEY}
# or {type: basic, username_env: USER_VAR, password_env: PASS_VAR}
# or {type: oauth2_client_credentials, token_url: ..., client_id_env: ..., client_secret_env: ..., scopes: []}
timeout: 30
retry: {attempts: 3}
rate_limit: {per_minute: 60}
operations:
  get_item:
    method: GET
    path: /items/{id}
    description: Fetch one item
    params:
      id: {in: path, type: string, required: true, description: Item id}
      expand: {in: query, type: boolean}
  create_item:
    method: POST
    path: /items
    description: Create an item
    permission: ask
    params:
      body: {in: body, type: object, required: true}
```

Read methods default to allow, writes to ask. Tools are named `<connector>__<operation>`.

MCP server:

```yaml
name: files
type: mcp
command: npx
args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
readonly_tools: [read_file]   # these skip approval
# or url: https://host/mcp, transport: streamable_http, headers_env: {Authorization: VAR_NAME}
```

Release test: `create_connector` makes one real read-only call (MCP: starts the server and lists tools) and releases
nothing if it fails. It uses the first read operation that needs no arguments, or `test: {operation: NAME, args: {...}}`.

Secrets are always named as environment variables. Ask the user to set them in the workspace `.env` before you call
`create_connector`. Afterwards, give the connector to an agent with `update_agent`.
