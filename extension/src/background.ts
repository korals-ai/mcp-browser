// The service worker: owns every live connection, answers the connect and
// status pages, and keeps itself awake while a relay is attached.
//
// Derived from packages/extension/src/background.ts in microsoft/playwright
// (Apache-2.0, Copyright (c) Microsoft Corporation).

import { debugLog } from './relayConnection';
import { PendingConnections } from './pendingConnection';
import { ConnectedTabGroup, cleanupStaleGroups, isNonDebuggableUrl, ungroupTabs, uniqueGroupStyle } from './connectedTabGroup';
import { KEEPALIVE_ALARM, KEEPALIVE_PERIOD_MINUTES } from './protocol';

export type PageMessage =
  | { type: 'connectionRequested'; relayUrl: string }
  | { type: 'getTabs' }
  // `tab` is the pick from the connect page; absent on the token path, where
  // the connect page itself becomes the first tab.
  | { type: 'connectToTab'; tab?: chrome.tabs.Tab; clientName?: string }
  | { type: 'getConnectionStatus' }
  | { type: 'disconnect'; connectionId: number }
  | { type: 'keepalive' };

class BridgeExtension {
  private _connections = new Map<number, ConnectedTabGroup>();
  private _lastConnectionId = 0;
  private _pendingConnections = new PendingConnections();
  // A worker restart loses every connection; groups left behind are stale.
  private _cleanupPromise: Promise<void>;

  constructor() {
    chrome.runtime.onMessage.addListener(this._onMessage.bind(this));
    chrome.action.onClicked.addListener(this._onActionClicked.bind(this));
    chrome.alarms.onAlarm.addListener(alarm => this._onAlarm(alarm));
    this._cleanupPromise = cleanupStaleGroups();
  }

  // Promise-returning listeners are not supported by chrome.runtime.onMessage.
  private _onMessage(message: PageMessage, sender: chrome.runtime.MessageSender, sendResponse: (response: unknown) => void): boolean {
    switch (message.type) {
      case 'connectionRequested': {
        const selectorTabId = sender.tab!.id!;
        this._releaseConnectPage(selectorTabId).then(() => {
          this._pendingConnections.create(selectorTabId, message.relayUrl);
          sendResponse({ success: true });
        });
        return true;
      }
      case 'getTabs':
        this._getTabs(sender.tab?.id).then(
            tabs => sendResponse({ success: true, tabs, currentTabId: sender.tab?.id }),
            (error: any) => sendResponse({ success: false, error: error.message }));
        return true;
      case 'connectToTab': {
        const selectedTab = (message.tab ?? sender.tab!) as chrome.tabs.Tab & { id: number };
        this._connectTab(sender.tab!.id!, selectedTab, message.clientName).then(
            () => sendResponse({ success: true }),
            (error: any) => sendResponse({ success: false, error: error.message }));
        return true;
      }
      case 'getConnectionStatus':
        sendResponse({
          connections: [...this._connections].map(([id, group]) => ({
            id,
            clientName: group.clientName,
            connectedTabIds: group.connectedTabIds(),
          })),
        });
        return false;
      case 'disconnect':
        this._connections.get(message.connectionId)?.close('User disconnected');
        sendResponse({ success: true });
        return false;
      case 'keepalive':
        // The connect page pings while it is open; receiving anything resets
        // the worker's idle timer.
        return false;
    }
    return false;
  }

  private async _connectTab(selectorTabId: number, tab: chrome.tabs.Tab & { id: number }, clientName: string | undefined): Promise<void> {
    try {
      await this._cleanupPromise;
      this._releaseTab(selectorTabId);
      if (tab.id !== selectorTabId && this._connectedTabIds().has(tab.id))
        throw new Error('This tab is already connected to another client');

      const connection = await this._pendingConnections.take(selectorTabId);
      if (!connection)
        throw new Error('Pending client connection closed');

      const id = ++this._lastConnectionId;
      const taken = [...this._connections.values()].map(group => group.groupStyle);
      const group = new ConnectedTabGroup(connection, tab, clientName, uniqueGroupStyle(clientName, taken), tabId => this._pendingConnections.has(tabId));
      group.onclose = () => {
        this._connections.delete(id);
        if (this._connections.size === 0)
          void chrome.alarms.clear(KEEPALIVE_ALARM);
      };
      this._connections.set(id, group);
      await chrome.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: KEEPALIVE_PERIOD_MINUTES });

      await Promise.all([
        chrome.tabs.update(tab.id, { active: true }),
        chrome.windows.update(tab.windowId, { focused: true }),
      ]).catch(() => {});

      if (tab.id !== selectorTabId)
        await chrome.tabs.remove(selectorTabId).catch(() => {});
    } catch (error: any) {
      debugLog(`Failed to connect tab ${tab.id}:`, error.message);
      throw error;
    }
  }

  private _onAlarm(alarm: chrome.alarms.Alarm): void {
    if (alarm.name !== KEEPALIVE_ALARM)
      return;
    if (this._connections.size === 0) {
      // Alarms are persistent: a browser restart (or a worker kill) loses
      // every connection but not the alarm, which would then wake this worker
      // every half minute forever. The first such wake retires it.
      void chrome.alarms.clear(KEEPALIVE_ALARM);
      return;
    }
    for (const group of this._connections.values())
      group.keepalive();
  }

  // Chrome may create the connect page inside an active client's group.
  private async _releaseConnectPage(tabId: number): Promise<void> {
    this._releaseTab(tabId);
    await ungroupTabs([tabId]);
  }

  private _releaseTab(tabId: number): void {
    for (const group of this._connections.values())
      group.releaseTab(tabId);
  }

  private async _getTabs(selectorTabId: number | undefined): Promise<chrome.tabs.Tab[]> {
    const tabs = await chrome.tabs.query({});
    const connectedTabIds = this._connectedTabIds();
    return tabs.filter(tab => !isNonDebuggableUrl(tab.url) && (tab.id === selectorTabId || !connectedTabIds.has(tab.id!)));
  }

  private _connectedTabIds(): Set<number> {
    return new Set([...this._connections.values()].flatMap(group => group.connectedTabIds()));
  }

  private async _onActionClicked(): Promise<void> {
    await chrome.tabs.create({ url: chrome.runtime.getURL('status.html'), active: true });
  }
}

new BridgeExtension();
