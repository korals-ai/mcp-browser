# mcp-browser

A real browser an agent can drive — and a human can watch. An [MCP](https://modelcontextprotocol.io) server speaking Streamable
HTTP: run it in a container, point your agent at `http://localhost:8096/mcp`.

A full Playwright-driven Chrome exposed as MCP tools: navigate, snapshot the page as structured
elements the agent reads, click, type, scroll, inspect tables and links, handle frames, dialogs,
downloads and uploads, and read console and network activity.

It serves a **second plane on the same port**: `/cobrowse`, a WebSocket that streams a live CDP
screencast of the session and accepts human input, so a person can watch the agent browse and
take over mid-task.

**And a viewer for it, at `/`.** Open `http://localhost:8096` after starting the container and you
get a live picture of the browser the agent is driving: click, type, scroll, navigate — you and the
agent share one session. This is the point of the tool. Driving a browser headlessly is a solved
problem; watching one work, and taking the keyboard when it gets stuck, is not.

The viewer is framework-free and served by the server itself, so there is nothing to build or host.
Its wire protocol, canvas painting and coordinate mapping (`viewer/src/`) are the same source files
the platform's own React viewer imports, so the two can never disagree about the format.

## Quickstart

```bash
docker compose up          # builds the image the first time
```

Then register it with your agent. Claude Code:

```bash
claude mcp add --transport http browser 'http://localhost:8096/mcp?chat_id=local'
```

…or in a client config:

```json
{"mcpServers": {"browser": {"type": "http", "url": "http://localhost:8096/mcp?chat_id=local"}}}
```

Then open **http://localhost:8096** in a browser tab to watch it work.

**The `?chat_id=` is required, not decorative.** Each id gets its own browser
session and its own persisted profile, and the server rejects a tool call that
carries none rather than quietly merging every caller into one shared profile
and one cookie jar. `local` is just the value this README uses on both sides —
the viewer reads the matching id from its own `?session=` (defaulting to
`local`), so if you change one, change the other.

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
