# mcp-browser

A real browser an agent can drive — and a human can watch. An [MCP](https://modelcontextprotocol.io) server speaking Streamable
HTTP: run it in a container, point your agent at `http://localhost:8096/mcp`.

A full Playwright-driven Chrome exposed as MCP tools: navigate, snapshot the page as structured
elements the agent reads, click, type, scroll, inspect tables and links, handle frames, dialogs,
downloads and uploads, and read console and network activity.

It serves a **second plane on the same port**: `/cobrowse`, a WebSocket that streams a live CDP
screencast of the session and accepts human input, so a person can watch the agent browse and
take over mid-task. The server side of that is here in full. The viewer that renders it is not —
it lives in a private application. The protocol is in `src/protocol.py` if you want to write one.

## Quickstart

```bash
docker compose up          # builds the image the first time
```

Then register it with your agent. Claude Code:

```bash
claude mcp add --transport http browser http://localhost:8096/mcp
```

…or in a client config:

```json
{"mcpServers": {"browser": {"type": "http", "url": "http://localhost:8096/mcp"}}}
```

## How files reach the tools

These tools take **paths, not uploads** — the agent names a file, the server
opens it in place and writes results back. Nothing but the path and the verdict
crosses the MCP wire, so a 200 MB file costs no tokens.

That means the container has to be able to see your files. `docker compose up`
mounts the directory you ran it from at `/work`, so tell the agent about
`/work/drawing.dxf`, not `~/drawing.dxf`. Mount somewhere else with
`WORKDIR=/path/to/project docker compose up`.

Files the tools create are written as your host user, not root:

```bash
MCP_UID=$(id -u) MCP_GID=$(id -g) docker compose up   # if your uid is not 1000
```

## Tools

- `browser_open`
- `browser_snapshot`
- `browser_click`
- `browser_type`
- `browser_read`
- `browser_find`
- `browser_screenshot`
- `browser_get_table`
- `browser_wait_for`
- `browser_upload_file`
- `browser_download`
- `browser_console`
- `browser_network`
- `browser_eval`
- `…and 22 more`

Each tool's own description and typed signature — what the agent actually reads
to decide when to call it — is in `src/server.py`.

## Requirements

Chrome for Testing, Xvfb and Playwright — the largest image here (~1.5 GB).

## Portal logins

`browser_login` reads credentials from a directory of files, one file per key, named by
`CONNECTORS_CREDS_DIR`. Put a `PORTAL_CREDENTIALS_JSON` file there holding a JSON array of
`{portal_id, login_url, username, password}` and the tool can log into those sites without the
password ever entering the agent's context. Leave the variable unset and the tool is simply
unavailable; everything else works.

## Contributing

Issues and PRs are welcome and read directly.

One thing to know before you send a PR: this repository is a **one-way mirror**
of a directory in a private monorepo, which stays canonical. Contributions are
applied there and reappear here on the next sync, so your change lands with your
authorship upstream but arrives in this repo's history inside a sync commit.
Nothing here is force-pushed away, but don't expect your PR to be merged with a
green button.

## License

Apache-2.0 — see [LICENSE](LICENSE).
