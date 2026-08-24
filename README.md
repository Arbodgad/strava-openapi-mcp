# strava-openapi-mcp

Local Python MCP server acting as a generic proxy between an MCP client—especially OpenCode—and the Strava REST API. Tools are not implemented endpoint by endpoint: they are generated at startup from Strava’s official Swagger 2.0 specification.

The repository contains a copy of the specification and its referenced schema documents. Startup therefore does not require Internet access to build the tool list. The `update-spec` command refreshes the user copy after validation.

## Architecture

`openapi.py` loads and validates Swagger, resolves local references, and normalizes operations. `tools.py` transforms each operation into an MCP tool with a generated JSON Schema. `client.py` builds URLs, parameters, JSON bodies, and multipart forms without knowing Strava endpoints individually. `auth.py` handles the local OAuth flow and token refresh. `server.py` exposes everything over MCP stdio, while `cli.py` provides maintenance commands.

Strava’s currently published specification is Swagger 2.0, with `info.version` `3.0.0`. The bundle is intentionally treated as replaceable data: if a new endpoint appears in the specification, it is discovered automatically.

## Prerequisites and local installation

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are recommended.

```bash
git clone https://github.com/<USER>/<REPO>.git
cd <REPO>
uv sync
uv run strava-mcp list-tools
```

Start the MCP server with:

```bash
uv run strava-mcp
```

The server remains active on the MCP stdin/stdout transport. Application logs are sent to stderr. No diagnostic log must be written to stdout during stdio transport.

## Create a Strava application

1. Open `https://www.strava.com/settings/api`.
2. Create an application and note its **Client ID** and **Client Secret**.
3. Strava accepts `localhost` and `127.0.0.1` as callback domains. The default callback is `http://127.0.0.1:8765/callback`.

Credentials can be provided through the environment:

```bash
export STRAVA_CLIENT_ID="..."
export STRAVA_CLIENT_SECRET="..."
```

Or in `~/.config/strava-mcp/credentials.json` with `0600` permissions:

```json
{
  "client_id": "...",
  "client_secret": "..."
}
```

Environment variables take precedence. The secret is never displayed or written to logs.

## OAuth

Run once:

```bash
strava-mcp auth
```

The browser opens the Strava authorization page. The local callback exchanges the authorization code for `access_token`, `refresh_token`, `expires_at`, and the granted scopes. Tokens are stored in `~/.config/strava-mcp/tokens.json` with `0600` permissions. The server automatically refreshes expired access tokens and persists a rotating refresh token when Strava returns one.

By default, all scopes declared by the specification are requested. To request a subset:

```bash
export STRAVA_OAUTH_SCOPES="activity:read,activity:write"
```

Official descriptions are analyzed to infer explicit scopes. Read endpoints accepting either `activity:read` or `activity:read_all` are represented as alternatives. A conditional scope—such as `activity:read_all` for a private activity—is shown to the LLM, and the original Strava error remains visible.

## Configuration

Supported variables:

| Variable | Default |
| --- | --- |
| `STRAVA_CLIENT_ID` | none, or `credentials.json` |
| `STRAVA_CLIENT_SECRET` | none, or `credentials.json` |
| `STRAVA_API_BASE_URL` | `https://www.strava.com/api/v3` |
| `STRAVA_OPENAPI_URL` | `https://developers.strava.com/swagger/swagger.json` |
| `STRAVA_OPENAPI_PATH` | `~/.config/strava-mcp/openapi.json` |
| `STRAVA_ALLOW_WRITE` | `true` |
| `STRAVA_ALLOW_DELETE` | `false` |
| `STRAVA_LOG_LEVEL` | `INFO` |
| `STRAVA_OAUTH_SCOPES` | all declared Strava scopes |
| `STRAVA_CALLBACK_HOST` / `STRAVA_CALLBACK_PORT` | `127.0.0.1` / `8765` |

The aliases `STRAVA_MCP_ALLOW_WRITE` and `STRAVA_MCP_ALLOW_DELETE` are also accepted. `strava-mcp show-config` displays only a non-secret configuration view.

The recommended values are `STRAVA_ALLOW_WRITE=true` and `STRAVA_ALLOW_DELETE=false`. POST, PUT, and PATCH methods are not blocked by default. DELETE methods are generated when the specification contains them, but filtered from the MCP tool list while `STRAVA_ALLOW_DELETE=false`.

## First startup

```bash
export STRAVA_CLIENT_ID="..."
export STRAVA_CLIENT_SECRET="..."
strava-mcp auth
strava-mcp list-tools
strava-mcp
```

The user specification copy takes precedence. If it does not exist, the bundled official specification is used without downloading anything at startup.

## Update the specification

```bash
strava-mcp update-spec
```

The command downloads `STRAVA_OPENAPI_URL`, validates the Swagger document, and then downloads referenced JSON documents. The existing copy is replaced only after the entire download and validation process succeeds. The reported version and number of referenced schemas are displayed.

To force a different path:

```bash
STRAVA_OPENAPI_PATH="$HOME/.config/strava-mcp/openapi.json" strava-mcp update-spec
```

## Direct installation with uvx from Git

The `pyproject.toml` declares the executable and all dependencies. No manual Python installation or clone is required:

```bash
uvx --from git+https://github.com/<USER>/<REPO> strava-mcp auth
uvx --from git+https://github.com/<USER>/<REPO> strava-mcp
```

To immediately use a new commit despite the uv cache:

```bash
uvx --refresh --from git+https://github.com/<USER>/<REPO> strava-mcp
```

## OpenCode configuration

Add the server to the OpenCode configuration:

```json
{
  "mcp": {
    "strava": {
      "type": "local",
      "command": [
        "uvx",
        "--from",
        "git+https://github.com/<USER>/<REPO>",
        "strava-mcp"
      ],
      "enabled": true
    }
  }
}
```

Export the variables in the environment that launches OpenCode, or use `credentials.json`, instead of committing secrets to this file. Run `strava-mcp auth` once for the same local account before starting OpenCode.

## Generated tools and examples

Names are derived from `operationId`, normalized to snake case, with an HTTP method prefix added only when necessary to avoid ambiguity. For example, with the current specification:

| Endpoint | Current generated tool |
| --- | --- |
| `GET /athlete` | `get_logged_in_athlete` |
| `GET /athlete/activities` | `get_logged_in_athlete_activities` |
| `GET /activities/{id}` | `get_activity_by_id` |
| `PUT /activities/{id}` | `put_update_activity_by_id` |
| `GET /activities/{id}/streams` | `get_activity_streams` |
| `GET /athletes/{id}/stats` | `get_stats` |

The `UpdatableActivity` body parameters are flattened into the PUT tool. The agent can therefore make conceptually equivalent calls:

```text
put_update_activity_by_id(id=123456789, name="Long Z2 run")
put_update_activity_by_id(id=123456789, description="Easy aerobic endurance session, good sensations.")
```

Other examples of natural-language requests:

- “List my latest running activities”: use `get_logged_in_athlete_activities`, then filter the returned results.
- “Read the details of activity 123”: use `get_activity_by_id(id=123)`.
- “Get the distance and heartrate streams for 123”: use `get_activity_streams(id=123, keys=["distance", "heartrate"], key_by_type=true)`.
- “Get my statistics”: obtain the authenticated athlete, then use `get_stats(id=...)`.

Pagination is fully controlled by the parameters in the specification (`page`, `per_page`, `before`, `after`, `page_size`, `after_cursor`, and so on). The server never automatically starts a long sequence of page requests.

## Writes and dangerous operations

MCP descriptions include `This operation modifies Strava data` for POST/PUT/PATCH and `WARNING` for DELETE. If `STRAVA_ALLOW_WRITE=false`, write tools return an explicit error. If `STRAVA_ALLOW_DELETE=false`, DELETE tools are absent from `list_tools` and direct calls are rejected.

HTTP errors preserve the status, endpoint, Strava message, and available rate-limit headers, for example:

```text
HTTP 401 Unauthorized
Endpoint: PUT /activities/{id}
Message: Invalid or expired token
```

A 204 response becomes the minimal object `{ "status": "success", "http_status": 204 }`. JSON responses retain Strava field names.

## CLI commands

```text
strava-mcp                       # MCP stdio server
strava-mcp auth                  # Browser OAuth + localhost callback
strava-mcp update-spec           # Validated update of the local copy
strava-mcp show-config           # Non-secret configuration
strava-mcp list-tools            # Method, endpoint, tool, and summary
strava-mcp list-tools --schemas  # Also display each inputSchema JSON
```

`list-tools --schemas` is useful for diagnosing an MCP client that rejects a
schema. JSON Schema keywords such as `required` are displayed at the relevant
schema level; a Strava property named `required` remains under `properties`.

## Tests and development

```bash
uv run pytest
uv run ruff check .
```

Tests use mocked HTTP transports and do not contact Strava. Integration tests against Strava are intentionally not run automatically.

## Troubleshooting

- `No Strava authorization found`: run `strava-mcp auth` with the correct credentials.
- `OAuth scope missing`: run `strava-mcp auth` again with the scope requested in `STRAVA_OAUTH_SCOPES`.
- `Spec update aborted`: the previous local copy remains intact; check the network or remove a custom `STRAVA_OPENAPI_PATH`.
- No DELETE tools: this is the default behavior; set `STRAVA_ALLOW_DELETE=true` and restart.
- MCP error related to stdout: do not add `print` calls to server code; logs must use logging configured for stderr.
- OAuth port already in use: set `STRAVA_CALLBACK_PORT` to an available port and, if necessary, register the localhost domain in the Strava application.

## Security

The client secret, access token, and refresh token are never included in logs, MCP descriptions, or error messages. Local credential and token files are ignored by Git and written with `0600` permissions. Never commit `.env`, `credentials.json`, or `tokens.json`.
