# MCP Browser Bridge

A Chrome extension that lets the browser tool's MCP server drive tabs you
choose in **your own browser** — your profile, your logins — instead of a
browser of its own. Nothing connects until you click **Allow** on a page the
server opens, and the server only ever sees the tabs in the group it is given.

## Install (unpacked)

```sh
cd extension
npm ci
npm run build          # → dist/
```

Then in Chrome: `chrome://extensions` → **Developer mode** → **Load unpacked** →
pick `dist/`. The extension id is `ipjfogjeagnpojnjignlhfapffkdpahi` on every
machine (the manifest pins it), which is what the server's
`BROWSER_EXTENSION_ID` expects.

Run the server on the same machine with `BROWSER_ATTACH=extension`,
`BROWSER_EXTENSION_ID=ipjfogjeagnpojnjignlhfapffkdpahi`, `BROWSER_EXECUTABLE_PATH`
pointing at your Chrome binary and `BROWSER_EXTENSION_TOKEN=""` (see the
top-level README). The first tool call opens the connect page.

## How a session works

1. The server starts a relay on `127.0.0.1` and opens
   `chrome-extension://…/connect.html?mcpRelayUrl=…&target=local` in your browser.
2. You pick the tab the agent starts from (**Allow & select**). The extension
   opens a WebSocket to the relay, attaches `chrome.debugger` to that tab and
   puts it in a tab group named `Agent · <client>`.
3. Drag more tabs into the group to hand them over; drag them out (or close
   them) to take them back. Closing the last one ends the session, and the
   server's next call says so.
4. The toolbar icon shows who is connected and has a **Disconnect** button.

The connect page shows a token. Put it in the server's
`BROWSER_EXTENSION_TOKEN` and later connections skip the dialog — the
connect page itself becomes the first tab. Treat it as a credential;
regenerate it from the page if it leaks.

## What the extension can and cannot do

- Everything the server needs runs through `chrome.debugger` on the tabs you
  gave it: reading, clicking, typing, screenshots, network bodies, frames,
  dialogs, popups. A command naming any other tab is refused here, inside the
  extension — the boundary does not depend on the server behaving.
- `download`: `chrome.debugger` cannot capture files, so a download goes where
  your browser saves downloads; the extension tells the server the path
  (`chrome.downloads`) and the server, on the same machine, copies it into its
  data folder. Downloads are browser-wide, so the server identifies its own by
  the id `chrome.downloads.onCreated` announces after its click — a file you
  save yourself while the agent works is never picked up.
- The `remote` target — an agent in a hosted workspace acting in this browser
  — is refused by name; only a relay on this machine is accepted.
- Firefox is not supported: there is no `chrome.debugger` there.

## Wire protocol

The relay speaks the Playwright Extension's protocol v2 (this extension began
as a fork of `packages/extension` in microsoft/playwright, Apache-2.0), plus:

- `chrome.downloads.search` as an allowed command, and
  `chrome.downloads.onCreated` / `onChanged` forwarded as events;
- `extension.keepalive`, sent every 30 s from a `chrome.alarms` alarm so the
  service worker stays awake on our terms, not Chrome's;
- a `target=local|remote` connect-page parameter.

The upstream extension still works with the relay, minus `download`.

## Develop

```sh
npm run typecheck   # tsc
npm run build       # esbuild → dist/
npm test            # protocol tests with a fake chrome.* (node --test)
```

`src/relayConnection.ts` is the contract (allow-list, attach/detach
bookkeeping, event filtering); `src/connectedTabGroup.ts` owns the tab group;
`src/background.ts` is the service worker; `src/ui/` the two pages.
