// One live link between this extension and a relay: a WebSocket the relay
// drives with {id, method, params} commands over an allow-listed set of
// chrome.* calls, and that the extension feeds with chrome.* events for the
// tabs it controls.
//
// Derived from packages/extension/src/relayConnection.ts in
// microsoft/playwright (Apache-2.0, Copyright (c) Microsoft Corporation);
// the additions are the download events/commands and the keepalive.

import {
  ALLOWED_CHROME_COMMANDS,
  DOWNLOAD_EVENT_METHODS,
  INITIALIZED_METHOD,
  KEEPALIVE_METHOD,
  TAB_EVENT_METHODS,
} from './protocol';

export function debugLog(...args: unknown[]): void {
  // eslint-disable-next-line no-console
  console.log('[MCP Browser Bridge]', ...args);
}

type ProtocolCommand = {
  id: number;
  method: string;
  params?: unknown[];
};

type ProtocolResponse = {
  id?: number;
  method?: string;
  params?: unknown;
  result?: unknown;
  error?: unknown;
};

// After a tab's debugger detaches with `target_closed`, the tab may still be
// alive (a cross-process navigation detaches and the tab is re-attachable a
// moment later). Wait this long, then look; keep a cooldown so a tab that
// keeps flapping is not re-attached in a loop.
const REATTACH_DELAY_MS = 150;
const REATTACH_VERIFY_MS = 2500;
const REATTACH_COOLDOWN_MS = 3000;

// Commands whose first positional argument names the tab they act on. The
// boundary the connect page promises ("other tabs stay out of reach") is
// enforced HERE, on every one of them: a relay that names a tab the user
// never handed over is refused, so the promise does not rest on the relay
// being ours and well-behaved.
const TAB_SCOPED_COMMANDS: ReadonlySet<string> = new Set([
  'chrome.debugger.attach',
  'chrome.debugger.detach',
  'chrome.debugger.sendCommand',
  'chrome.tabs.remove',
]);

function tabIdForCommand(method: string, args: unknown[]): number | undefined {
  if (!TAB_SCOPED_COMMANDS.has(method))
    return undefined;
  const first = args[0];
  if (typeof first === 'number')
    return first;                                     // chrome.tabs.remove(tabId)
  return (first as chrome.debugger.Debuggee | undefined)?.tabId;
}

export class RelayConnection {
  private _ws: WebSocket;
  // Tabs whose debugger this connection has attached.
  private _attachedTabs = new Set<number>();
  // Tabs the USER handed to this connection (the group's picks and drag-ins,
  // popups they opened, tabs the client asked us to create) — the only tabs
  // a command may name. A tab leaving the group leaves this set with it.
  private _offeredTabs = new Set<number>();
  // Once at least one tab was attached, detaching the last one ends the connection.
  private _hasEverAttached = false;
  private _eventListeners: Array<{ remove: () => void }> = [];
  private _closed = false;
  private _pendingReattach = new Set<number>();
  private _recentReattach = new Set<number>();

  onclose?: () => void;
  ontabattached?: (tabId: number) => void;
  ontabdetached?: (tabId: number) => void;

  get attachedTabs(): ReadonlySet<number> {
    return this._attachedTabs;
  }

  get closed(): boolean {
    return this._closed;
  }

  constructor(ws: WebSocket) {
    this._ws = ws;
    this._installEventForwarders();
    this._ws.onmessage = this._onMessage.bind(this);
    this._ws.onclose = () => this._onClose();
  }

  // Ends the initial-tab handshake: the relay holds Playwright's CDP traffic
  // until it sees this, so `Target.setAutoAttach` is answered from a
  // populated tab model.
  didInitialize(): void {
    this._sendMessage({ method: INITIALIZED_METHOD, params: [] });
  }

  keepalive(): void {
    this._sendMessage({ method: KEEPALIVE_METHOD, params: [] });
  }

  close(message: string): void {
    this._ws.close(1000, message);
    // ws.onclose fires asynchronously; run the bookkeeping now so no event
    // is forwarded to a socket that is closing.
    this._onClose();
  }

  // A tab joined the group (the initial pick, a later drag-in, a re-attach):
  // announce it as a created tab; the relay answers with chrome.debugger.attach.
  attachTab(tab: chrome.tabs.Tab): void {
    if (this._closed || tab.id === undefined || this._attachedTabs.has(tab.id))
      return;
    this._offeredTabs.add(tab.id);
    this._sendMessage({ method: 'chrome.tabs.onCreated', params: [tab] });
  }

  // A tab left the group. chrome.debugger.detach fires no onDetach for the
  // caller, so the event is synthesised for the relay.
  detachTab(tabId: number): void {
    if (this._closed || !this._attachedTabs.has(tabId))
      return;
    this._withdrawTab(tabId);
    chrome.debugger.detach({ tabId }).catch(error => debugLog('Error detaching tab:', error));
    this._notifyTabDetached(tabId);
    this._sendMessage({ method: 'chrome.debugger.onDetach', params: [{ tabId }, 'target_closed'] });
    this._checkLastTabDetached();
  }

  private _notifyTabAttached(tabId: number): void {
    this._attachedTabs.add(tabId);
    this._hasEverAttached = true;
    this._pendingReattach.delete(tabId);
    this.ontabattached?.(tabId);
  }

  private _notifyTabDetached(tabId: number): void {
    this._attachedTabs.delete(tabId);
    this.ontabdetached?.(tabId);
  }

  // The tab is out of the group (dragged out, closed, the connection ending):
  // commands naming it are refused from now on. A re-attach re-offers it
  // through attachTab.
  private _withdrawTab(tabId: number): void {
    this._offeredTabs.delete(tabId);
  }

  private _installEventForwarders(): void {
    for (const fullMethod of TAB_EVENT_METHODS)
      this._listen(fullMethod, (...args: unknown[]) => this._onTabEvent(fullMethod, args));
    for (const fullMethod of DOWNLOAD_EVENT_METHODS) {
      // Optional: a build without the downloads permission simply forwards none.
      if (!chrome.downloads)
        break;
      this._listen(fullMethod, (...args: unknown[]) => this._sendMessage({ method: fullMethod, params: args }));
    }
  }

  private _listen(fullMethod: string, listener: (...args: any[]) => void): void {
    const target = resolveChromeMember(fullMethod);
    target.obj[target.name].addListener(listener);
    this._eventListeners.push({ remove: () => target.obj[target.name].removeListener(listener) });
  }

  private _onClose(): void {
    if (this._closed)
      return;
    this._closed = true;
    this._pendingReattach.clear();
    this._recentReattach.clear();
    for (const l of this._eventListeners)
      l.remove();
    this._eventListeners = [];
    for (const tabId of [...this._attachedTabs]) {
      chrome.debugger.detach({ tabId }).catch(() => {});
      this._notifyTabDetached(tabId);
    }
    this._offeredTabs.clear();
    this.onclose?.();
  }

  private _checkLastTabDetached(): void {
    if (this._hasEverAttached && this._attachedTabs.size === 0 && this._pendingReattach.size === 0)
      this.close('All controlled tabs detached');
  }

  private _onTabEvent(fullMethod: string, args: unknown[]): void {
    const tabId = tabIdForEventArgs(fullMethod, args);
    if (tabId === undefined || !this._attachedTabs.has(tabId))
      return;
    if (fullMethod === 'chrome.tabs.onCreated') {
      // A popup an attached tab opened: the user's own click opened it, so it
      // is offered like any tab they handed over (the id above is the OPENER).
      const popupId = (args[0] as chrome.tabs.Tab).id;
      if (popupId !== undefined)
        this._offeredTabs.add(popupId);
    }
    if (fullMethod === 'chrome.tabs.onRemoved')
      this._withdrawTab(tabId);
    this._sendMessage({ method: fullMethod, params: args });
    // chrome.debugger.onDetach is the one source of truth for detach bookkeeping.
    if (fullMethod === 'chrome.debugger.onDetach') {
      const reason = args[1] as string | undefined;
      this._notifyTabDetached(tabId);
      if (reason === 'target_closed' && this._maybeScheduleReattach(tabId))
        return;
      this._checkLastTabDetached();
    }
  }

  private _maybeScheduleReattach(tabId: number): boolean {
    if (this._closed)
      return false;
    if (this._recentReattach.has(tabId)) {
      debugLog(`Not re-attaching tab ${tabId}: re-detached within ${REATTACH_COOLDOWN_MS}ms`);
      return false;
    }
    this._recentReattach.add(tabId);
    setTimeout(() => this._recentReattach.delete(tabId), REATTACH_COOLDOWN_MS);
    this._pendingReattach.add(tabId);
    setTimeout(() => void this._tryReattach(tabId), REATTACH_DELAY_MS);
    return true;
  }

  private _reattachAborted(tabId: number): boolean {
    return this._closed || !this._pendingReattach.has(tabId);
  }

  private async _tryReattach(tabId: number): Promise<void> {
    if (this._reattachAborted(tabId))
      return;
    let tab: chrome.tabs.Tab | undefined;
    try {
      tab = await chrome.tabs.get(tabId);
    } catch {
      this._pendingReattach.delete(tabId);
      this._checkLastTabDetached();
      return;
    }
    if (this._reattachAborted(tabId))
      return;
    if (this._attachedTabs.has(tabId)) {
      this._pendingReattach.delete(tabId);
      return;
    }
    this.attachTab(tab);
    setTimeout(() => {
      if (this._reattachAborted(tabId))
        return;
      this._pendingReattach.delete(tabId);
      if (!this._attachedTabs.has(tabId))
        this._checkLastTabDetached();
    }, REATTACH_VERIFY_MS);
  }

  private _onMessage(event: MessageEvent): void {
    this._onMessageAsync(event).catch(e => debugLog('Error handling message:', e));
  }

  private async _onMessageAsync(event: MessageEvent): Promise<void> {
    let message: ProtocolCommand;
    try {
      message = JSON.parse(event.data);
    } catch (error: any) {
      debugLog(`Error parsing message ${event.data}:`, error);
      this._sendMessage({ error: { code: -32700, message: `Error parsing message: ${error.message}` } });
      return;
    }
    const response: ProtocolResponse = { id: message.id };
    try {
      response.result = await this.handleCommand(message.method, message.params ?? []);
    } catch (error: any) {
      // Method and id only: params carry what the client types into the page,
      // and `login` fills a password through chrome.debugger.sendCommand.
      debugLog(`Error handling command ${message.method} (id ${message.id}):`, error);
      response.error = error.message;
    }
    this._sendMessage(response);
  }

  // Public for the tests: the allow-list and the attach bookkeeping are the
  // contract, the socket is plumbing.
  async handleCommand(method: string, args: unknown[]): Promise<unknown> {
    if (!ALLOWED_CHROME_COMMANDS.has(method))
      throw new Error(`Unknown method: ${method}`);
    const tabId = tabIdForCommand(method, args);
    if (tabId !== undefined && !this._offeredTabs.has(tabId))
      throw new Error(`Tab ${tabId} was not handed to this connection`);
    const result = await invokeChromeMethod(method, args);
    if (method === 'chrome.debugger.attach' && tabId !== undefined)
      this._notifyTabAttached(tabId);
    if (method === 'chrome.tabs.create') {
      // The client asked for the tab, so it is offered — otherwise nothing
      // could drive the tab it just opened.
      const created = (result as chrome.tabs.Tab | undefined)?.id;
      if (created !== undefined)
        this._offeredTabs.add(created);
    }
    return result ?? {};
  }

  private _sendMessage(message: ProtocolResponse): void {
    if (this._ws.readyState === WebSocket.OPEN)
      this._ws.send(JSON.stringify(message));
  }
}

// Which tab an event is about, so only controlled tabs are forwarded.
export function tabIdForEventArgs(fullMethod: string, args: unknown[]): number | undefined {
  switch (fullMethod) {
    case 'chrome.debugger.onEvent':
    case 'chrome.debugger.onDetach':
      return (args[0] as chrome.debugger.Debuggee | undefined)?.tabId;
    case 'chrome.tabs.onCreated':
      // Only a popup an attached tab opened; the relay decides what to do with it.
      return (args[0] as chrome.tabs.Tab).openerTabId;
    case 'chrome.tabs.onRemoved':
      return args[0] as number;
  }
  return undefined;
}

// ─── Reflective chrome.* invocation ──────────────────────────────────────

function resolveChromeMember(fullMethod: string): { obj: any; name: string } {
  const parts = fullMethod.split('.');
  if (parts[0] !== 'chrome' || parts.length < 3)
    throw new Error(`Invalid chrome method: ${fullMethod}`);
  let obj: any = chrome;
  for (let i = 1; i < parts.length - 1; i++) {
    obj = obj?.[parts[i]];
    if (obj === undefined)
      throw new Error(`Unknown chrome path: ${parts.slice(0, i + 1).join('.')}, calling ${fullMethod}`);
  }
  return { obj, name: parts[parts.length - 1] };
}

async function invokeChromeMethod(fullMethod: string, args: unknown[]): Promise<unknown> {
  const { obj, name } = resolveChromeMember(fullMethod);
  const fn = obj[name] as (...a: unknown[]) => unknown;
  if (typeof fn !== 'function')
    throw new Error(`Not a function: ${fullMethod}`);
  return await fn.apply(obj, args);
}
