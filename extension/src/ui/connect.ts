// The connect page: a client (the browser tool's server) opened this URL
// asking to attach. Checks the request, then either connects on a matching
// token or shows the tab picker. Nothing dials out before the user clicks.
//
// Derived from packages/extension/src/ui/connect.tsx in microsoft/playwright
// (Apache-2.0, Copyright (c) Microsoft Corporation).

import { PROTOCOL_VERSION, TARGETS, type Target } from '../protocol';
import { getOrCreateAuthToken, renderAuthTokenSection } from './authToken';
import { renderTabItem } from './tabItem';

const params = new URLSearchParams(window.location.search);
const relayUrl = params.get('relayUrl') ?? params.get('mcpRelayUrl');
const clientName = parseClientName(params.get('client'));

const statusEl = document.getElementById('status')!;
const bodyEl = document.getElementById('body')!;
const tabsEl = document.getElementById('tabs')!;
const tokenEl = document.getElementById('token')!;

function parseClientName(raw: string | null): string {
  if (!raw)
    return 'unknown';
  try {
    const parsed = JSON.parse(raw);
    return typeof parsed?.name === 'string' && parsed.name ? parsed.name : 'unknown';
  } catch {
    return 'unknown';
  }
}

function setStatus(kind: 'info' | 'error' | 'connected', message: string): void {
  statusEl.className = `status status-${kind}`;
  statusEl.textContent = message;
}

function fail(message: string): void {
  setStatus('error', message);
  bodyEl.hidden = true;
}

// Every refusal below is a sentence the person can act on; a silent page was
// the failure mode this replaces.
function checkRequest(): string | undefined {
  if (!relayUrl)
    return 'This page needs a relayUrl parameter — open it from the browser tool, not by hand.';
  let host: string;
  try {
    host = new URL(relayUrl).hostname;
  } catch (e) {
    return `Invalid relayUrl: ${relayUrl}. ${e}`;
  }
  if (host !== '127.0.0.1' && host !== '[::1]')
    return `Only a relay on this machine (127.0.0.1 or [::1]) is accepted; the request named ${host}.`;
  const target = (params.get('target') ?? 'local') as Target;
  if (!TARGETS.includes(target))
    return `Unknown target "${target}"; this extension knows ${TARGETS.join(' and ')}.`;
  if (target === 'remote')
    return 'A remote target (an agent in a hosted workspace acting in this browser) is not available in this version; only a local relay is.';
  const parsedVersion = parseInt(params.get('protocolVersion') ?? '', 10);
  const requested = isNaN(parsedVersion) ? 1 : parsedVersion;
  if (requested > PROTOCOL_VERSION)
    return `The client speaks protocol v${requested}; this extension speaks v${PROTOCOL_VERSION}. Update the extension.`;
  if (requested < PROTOCOL_VERSION)
    return `The client speaks protocol v${requested}; this extension needs v${PROTOCOL_VERSION}. Update the browser tool.`;
  return undefined;
}

async function loadTabs(): Promise<void> {
  const response = await chrome.runtime.sendMessage({ type: 'getTabs' });
  if (!response?.success) {
    fail(`Could not list tabs: ${response?.error ?? 'no answer from the extension'}`);
    return;
  }
  tabsEl.innerHTML = '';
  for (const tab of response.tabs as chrome.tabs.Tab[]) {
    const item = renderTabItem(tab, 'Allow & select', () => connectToTab(tab));
    tabsEl.appendChild(item);
  }
}

async function connectToTab(tab?: chrome.tabs.Tab): Promise<void> {
  bodyEl.hidden = true;
  try {
    const response = await chrome.runtime.sendMessage({ type: 'connectToTab', tab, clientName });
    if (response?.success)
      setStatus('connected', `"${clientName}" connected.`);
    else
      fail(response?.error || `"${clientName}" failed to connect.`);
  } catch (e: any) {
    fail(`"${clientName}" failed to connect: ${e?.message ?? e}`);
  }
}

async function main(): Promise<void> {
  const refusal = checkRequest();
  if (refusal) {
    fail(refusal);
    return;
  }
  setStatus('info', `"${clientName}" wants to attach to this browser.`);
  await chrome.runtime.sendMessage({ type: 'connectionRequested', relayUrl });
  const expectedToken = getOrCreateAuthToken();
  const token = params.get('token');
  if (token && token === expectedToken) {
    await connectToTab();
    return;
  }
  if (token) {
    fail('The token the client sent does not match this browser\'s token. Copy the current one from the box below into the server\'s configuration.');
    renderAuthTokenSection(tokenEl);
    tokenEl.hidden = false;
    return;
  }
  renderAuthTokenSection(tokenEl);
  tokenEl.hidden = false;
  bodyEl.hidden = false;
  await loadTabs();
}

void main();
// While this page is open, keep the worker awake so the pending connection survives.
setInterval(() => { chrome.runtime.sendMessage({ type: 'keepalive' }).catch(() => {}); }, 20_000);
