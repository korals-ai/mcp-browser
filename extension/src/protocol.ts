// The wire contract between this extension and the relay inside the browser
// tool's server (src/cdp_relay.py). It is the Playwright Extension's protocol
// v2 — the relay speaks it verbatim — plus three additions of our own:
// downloads, an application-level keepalive, and a `target` parameter on the
// connect URL. A relay written for the upstream extension still works with
// this one; the additions are extra methods it may simply never call.

export const PROTOCOL_VERSION = 2;

// The connect page's target picker. `local` is the relay on this machine
// (loopback only). `remote` — a relay reached through a hosted workspace so a
// tenant's agent acts in this browser — is not built; naming it here is what
// makes a client that asks for it fail with a sentence instead of a shrug.
export const TARGETS = ['local', 'remote'] as const;
export type Target = (typeof TARGETS)[number];

// Shown on the connect page as the variable to set on the server side.
export const TOKEN_ENV_NAME = 'BROWSER_EXTENSION_TOKEN';

// chrome.* calls the relay may invoke, resolved reflectively with positional
// params. Everything the debugger bridge needs, plus what `download` needs
// to find the file Chrome just saved.
export const ALLOWED_CHROME_COMMANDS: ReadonlySet<string> = new Set([
  'chrome.debugger.attach',
  'chrome.debugger.detach',
  'chrome.debugger.sendCommand',
  'chrome.tabs.create',
  'chrome.tabs.remove',
  'chrome.downloads.search',
]);

// chrome.* events forwarded to the relay while a connection is up. Tab events
// are filtered to the tabs the connection controls; download events carry no
// tab, so they are forwarded whole and the relay identifies its own by the id
// onCreated announces (a download list is browser-wide — matching on "the
// newest one" would hand the agent a file the user started).
export const TAB_EVENT_METHODS = [
  'chrome.debugger.onEvent',
  'chrome.debugger.onDetach',
  'chrome.tabs.onCreated',
  'chrome.tabs.onRemoved',
] as const;

export const DOWNLOAD_EVENT_METHODS = ['chrome.downloads.onCreated', 'chrome.downloads.onChanged'] as const;

// Sent by the extension without being asked.
export const INITIALIZED_METHOD = 'extension.initialized';
export const KEEPALIVE_METHOD = 'extension.keepalive';

// The MV3 service worker is put to sleep after ~30 s without an event. The
// relay socket alone keeps it up on current Chrome, but that is Chrome's
// behaviour, not a promise: an alarm every half minute (the minimum period)
// is ours.
export const KEEPALIVE_ALARM = 'relay-keepalive';
export const KEEPALIVE_PERIOD_MINUTES = 0.5;
