# mcp-browser

A real browser an agent can drive — and a human can watch. An [MCP](https://modelcontextprotocol.io) server speaking Streamable
HTTP: run it in a container, point your agent at `http://localhost:8096/mcp`.
It works with the agent you already use — Claude Code, Codex, Gemini CLI,
opencode, Cursor, VS Code, or any MCP client that talks to remote servers.

A full Playwright-driven Chrome exposed as MCP tools **with the Claude-in-Chrome
extension's tool contract, verbatim** — `computer`, `read_page`, `find`,
`form_input`, `navigate`, `browser_batch`, … — so a skill written for the
extension drives this browser unchanged. The page is read as an accessibility
tree of `[ref=eN]` elements; a mutating call returns only what it did plus what
changed; `browser_batch` runs several calls in one round trip.

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

**1. Choose how `find` works.** `find` turns a description ("the search box in
the header") into an element on the page.

```bash
cp .env.example .env
```

As copied, `.env` selects **literal matching**: no model, no key, no cost —
`find` matches the page's own words. For fuzzy descriptions, give it a small
model instead: any endpoint that speaks the Anthropic Messages API works (see
[Choosing a model for `find`](#choosing-a-model-for-find)). The quickest is
[OpenRouter](https://openrouter.ai) — one key, hundreds of models, some of them
free:

```bash
BROWSER_FIND_INFERENCE_URL=https://openrouter.ai/api
BROWSER_FIND_INFERENCE_KEY=<your key from https://openrouter.ai/keys>
BROWSER_FIND_MODEL=anthropic/claude-haiku-4.5
```

**2. Start the server.**

```bash
docker compose up          # builds the image the first time
```

**3. Connect your agent** to `http://localhost:8096/mcp?chat_id=local`:

Claude Code

```bash
claude mcp add --transport http browser 'http://localhost:8096/mcp?chat_id=local'
```

Codex

```bash
codex mcp add browser --url 'http://localhost:8096/mcp?chat_id=local'
```

Gemini CLI

```bash
gemini mcp add --transport http browser 'http://localhost:8096/mcp?chat_id=local'
```

opencode — in `opencode.json`:

```json
{"mcp": {"browser": {"type": "remote", "url": "http://localhost:8096/mcp?chat_id=local"}}}
```

Cursor — in `~/.cursor/mcp.json`:

```json
{"mcpServers": {"browser": {"url": "http://localhost:8096/mcp?chat_id=local"}}}
```

VS Code — in `.vscode/mcp.json`:

```json
{"servers": {"browser": {"type": "http", "url": "http://localhost:8096/mcp?chat_id=local"}}}
```

Any other client: a Streamable HTTP MCP server at that URL.

**4. Watch it work** — open **http://localhost:8096** in a browser tab.

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
mounts `./work` (next to this README) at `/work`: put the files the agent should
use there and tell it about `/work/drawing.dxf`, not `~/drawing.dxf`. Downloads
and saved screenshots land there too, and so does the browser profile
(`/work/.cobrowse/`), so logins survive a restart. Mount another folder with
`WORKDIR=/path/to/project docker compose up`.

**Everything in that folder is reachable by the agent's file tools** — and an
agent that a web page has talked into it could attach any of those files to a
form on that page. Mount a folder of working files, never your home directory
or a folder that holds keys; this repo's own `.env` sits outside `./work` for
exactly that reason.

Files the tools create are written as your host user, not root:

```bash
MCP_UID=$(id -u) MCP_GID=$(id -g) docker compose up   # if your uid is not 1000
```

## Tools

The 15 the extension has, same names, same argument shapes:

- `tabs_context_mcp` / `tabs_create_mcp` / `tabs_close_mcp` — every per-tab tool
  takes the numeric `tabId`
- `navigate` — `url`, `"back"` or `"forward"`; returns where it landed, and a
  `page_state` that names a bot wall
- `read_page` — the accessibility tree with refs (`filter: interactive|all`,
  `max_chars`, `depth`, `ref_id`, `boxes`)
- `get_page_text` — the readable text
- `find` — refs matching a description: a literal tier over the tree's own
  words (role + name, "box"/"button"/"link" read as roles), then a model tier
  if configured (below)
- `form_input` — set a field / checkbox / select by ref
- `computer` — `left_click`, `right_click`, `double_click`, `triple_click`,
  `type`, `key`, `screenshot`, `zoom`, `wait`, `scroll`, `scroll_to`, `hover`,
  `left_click_drag`; by `ref` or by `coordinate`
- `read_console_messages` / `read_network_requests`
- `file_upload` / `resize_window` / `javascript_tool`
- `browser_batch` — a list of `{name, input}` run in order, stopping at the
  first error

And what a sandboxed browser needs that the extension does not:

- `get_network_request` — one request's headers and body by index (a `reason`
  is required and logged; cookie/authorization headers are redacted)
- `login` — portal credentials injected server-side (below)
- `wait_for` — text / selector / url / response
- `download`, `list_frames` / `switch_frame`, `set_dialog_mode` / `last_dialog`
- `run_recipe` — replay a stored batch (`{name, input, target?}` steps) with no
  model between the steps

Refs are valid for one document: after a navigation, `read_page` again — a stale
ref is refused by name, never guessed. Each tool's own description and typed
signature — what the agent actually reads to decide when to call it — is in
`src/server.py`.

## Configuration

Every variable is required; the server fails at startup on a missing one rather
than picking a default.

| variable | meaning |
| --- | --- |
| `BROWSER_FIND_INFERENCE_URL` | base URL of an endpoint speaking the Anthropic Messages API (`/v1/messages` is appended) for `find`'s model tier — e.g. `https://openrouter.ai/api`, `https://api.anthropic.com`; `""` = literal matching only |
| `BROWSER_FIND_INFERENCE_KEY` | its API key (`""` when the URL is empty) |
| `BROWSER_FIND_MODEL` | the model `find` asks, in that provider's own naming — `anthropic/claude-haiku-4.5` on OpenRouter, `claude-haiku-4-5-20251001` on Anthropic; required with a URL, `""` without one |
| `BROWSER_ATTACH` | `launch` (the server runs its own Chromium — what the image does) or `extension` (drive YOUR browser through a browser extension; see below) |
| `BROWSER_EXTENSION_ID` | extension mode only: the extension you installed — `ipjfogjeagnpojnjignlhfapffkdpahi` (this repo's `extension/`) or `mmlmfjhmonkocbjadbfplnigmagldckm` (the Playwright Extension) |
| `BROWSER_EXTENSION_TOKEN` | extension mode only: the token the extension's connect page shows, so it connects without asking each time; `""` = approve in the browser every time |
| `BROWSER_HEADLESS`, `BROWSER_EXECUTABLE_PATH`, `BROWSER_MAX_SESSIONS`, `BROWSER_VIEWER_DIR`, `WORKSPACE_TOOL_HOST`, `WORKSPACE_TOOL_PORT`, `CONNECTORS_CREDS_DIR` | see `docker-compose.yml` |

## Choosing a model for `find`

`find` first matches the accessibility tree's own words; only a query those
words cannot answer goes to the model, with the tree and the query, and the
answer is checked against the refs that exist — a model can pick the wrong
element, but never one that is not on the page. So the model tier needs a
small, fast model, not a large one.

- **Provider.** Any endpoint that speaks the Anthropic Messages API:
  [OpenRouter](https://openrouter.ai/models), Anthropic itself, or a gateway
  you run. Switching is three variables — nothing in the server knows which
  provider it is talking to.
- **Model.** `anthropic/claude-haiku-4.5` on OpenRouter
  (`claude-haiku-4-5-20251001` on Anthropic) is what `find` is written against.
  OpenRouter's free models (ids ending `:free`) cost nothing, but they are
  rate-limited, the list changes, and they have not been measured on `find`
  here — try one on your own sites before relying on it.
- **Attribution.** Model calls carry `HTTP-Referer` and `X-OpenRouter-Title`
  headers naming this project, which OpenRouter uses for its public app
  rankings; other providers ignore them. The content sent is the page's
  accessibility tree (with any password `login` typed blanked out) and your
  query — nothing else.

## Driving your own browser (extension mode)

The server can attach to a browser you already use instead of running its
own — every tool then acts in your tabs, with your logins. The bridge is the
**MCP Browser Bridge** extension in [`extension/`](extension/README.md)
(Chromium browsers only — it needs `chrome.debugger`); the
[Playwright Extension](https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm)
it was forked from also works, minus `download`.

1. Build and load the extension: `cd extension && npm ci && npm run build`,
   then `chrome://extensions` → Developer mode → Load unpacked → `dist/`.
2. Run the server on the host (not in Docker: it has to open a page in your
   browser) with `BROWSER_ATTACH=extension`,
   `BROWSER_EXTENSION_ID=ipjfogjeagnpojnjignlhfapffkdpahi`,
   `BROWSER_EXECUTABLE_PATH` set to that browser's binary, and
   `BROWSER_EXTENSION_TOKEN=""`.
3. The first tool call opens the extension's connect page; click
   **Allow & select** on the tab the agent should start from. That page shows
   a `BROWSER_EXTENSION_TOKEN=…` value — put it in the server's environment
   and later connections skip the dialog.

The tools work the same, with these differences: `download` saves through
your browser (wherever it saves downloads) and the server copies the file
from there — it needs the server on the same machine; `resize_window`
emulates the size inside the window rather than resizing it; and the
co-browse viewer is off — you are looking at the browser. Disconnecting the
extension (its toolbar button, or closing the last tab you gave it) ends the
session: the next tool call reports it and the one after that reconnects.

The connect page is opened by launching the browser binary with its URL, and
with a token set the token is IN that URL. When the browser is already
running, the launched process hands the URL over and exits at once; when it is
not, that process IS the browser and its command line (the token included)
stays readable to other users of the machine for as long as it runs. Have the
browser running first, or use `BROWSER_EXTENSION_TOKEN=""` and click.

## Requirements

Chrome for Testing, Xvfb and Playwright — the largest image here (~1.5 GB).

## Portal logins

`login` reads credentials from a directory of files, one file per key, named by
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
