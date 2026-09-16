// The Chrome tab group of one live connection — the single source of truth
// for which tabs the client may touch. Drag a tab in and it is attached;
// drag it out (or close it) and it is detached; a tab the relay creates
// itself lands in the group too. Everything else in the browser stays
// invisible to the client.
//
// Derived from packages/extension/src/connectedTabGroup.ts in
// microsoft/playwright (Apache-2.0, Copyright (c) Microsoft Corporation).

import { RelayConnection, debugLog } from './relayConnection';

const GROUP_TITLE_PREFIX = 'Agent · ';
// Green first, so a lone connection keeps one familiar look.
const GROUP_COLORS: GroupColor[] = ['green', 'blue', 'purple', 'orange', 'pink', 'cyan', 'yellow', 'red'];
const NON_DEBUGGABLE_SCHEMES = ['chrome:', 'edge:', 'devtools:'];
const CONNECTED_BADGE = { text: '✓', color: '#4CAF50', title: 'Controlled by an agent' };

export function isNonDebuggableUrl(url: string | undefined): boolean {
  return !!url && NON_DEBUGGABLE_SCHEMES.some(s => url.startsWith(s));
}

type GroupColor = chrome.tabGroups.ColorEnum;

export type GroupStyle = {
  title: string;
  color: GroupColor;
};

export function uniqueGroupStyle(clientName: string | undefined, taken: readonly GroupStyle[]): GroupStyle {
  const titles = new Set(taken.map(style => style.title));
  const base = GROUP_TITLE_PREFIX + (clientName || 'unknown');
  let title = base;
  for (let i = 2; titles.has(title); i++)
    title = `${base} (${i})`;
  const colors = new Set(taken.map(style => style.color));
  const color = GROUP_COLORS.find(candidate => !colors.has(candidate)) ?? GROUP_COLORS[0];
  return { title, color };
}

// A service worker restart forgets every connection, so any group with our
// title is stale: release its tabs before accepting new connections.
export async function cleanupStaleGroups(): Promise<void> {
  try {
    const groups = await chrome.tabGroups.query({});
    const stale = groups.filter(g => g.title?.startsWith(GROUP_TITLE_PREFIX));
    const tabsPerGroup = await Promise.all(stale.map(g => chrome.tabs.query({ groupId: g.id })));
    const tabIds = tabsPerGroup.flat().map(t => t.id).filter((id): id is number => id !== undefined);
    if (tabIds.length)
      await ungroupTabs(tabIds);
  } catch (error: any) {
    debugLog('Error cleaning up stale groups:', error);
  }
}

export class ConnectedTabGroup {
  readonly clientName: string | undefined;
  readonly groupStyle: GroupStyle;
  private _connection: RelayConnection;
  private _isTabReserved: (tabId: number) => boolean;
  private _groupId: number | null = null;
  // Group membership as Chrome reported it, so the hot path stays synchronous.
  private _groupTabIds: Set<number> = new Set();
  private _onTabUpdatedListener: (tabId: number, changeInfo: chrome.tabs.TabChangeInfo, tab: chrome.tabs.Tab) => void;
  private _onTabRemovedListener: (tabId: number) => void;

  onclose?: () => void;

  constructor(connection: RelayConnection, selectedTab: chrome.tabs.Tab, clientName: string | undefined, groupStyle: GroupStyle, isTabReserved: (tabId: number) => boolean) {
    this.clientName = clientName;
    this.groupStyle = groupStyle;
    this._isTabReserved = isTabReserved;
    this._connection = connection;
    this._connection.onclose = () => this._onConnectionClose();
    this._connection.ontabattached = (tabId: number) => this._onTabAttached(tabId);
    this._connection.ontabdetached = (tabId: number) => this._onTabDetached(tabId);
    this._onTabUpdatedListener = this._onTabUpdated.bind(this);
    this._onTabRemovedListener = this._onTabRemoved.bind(this);
    chrome.tabs.onUpdated.addListener(this._onTabUpdatedListener);
    chrome.tabs.onRemoved.addListener(this._onTabRemovedListener);
    // Seed the relay with the picked tab, then close the handshake: the relay
    // holds CDP traffic until `didInitialize`, so it answers
    // `Target.setAutoAttach` from a populated tab model.
    this._connection.attachTab(selectedTab);
    this._connection.didInitialize();
  }

  connectedTabIds(): number[] {
    return [...this._groupTabIds];
  }

  close(reason: string): void {
    this._connection.close(reason);
  }

  keepalive(): void {
    this._connection.keepalive();
  }

  releaseTab(tabId: number): void {
    if (!this._groupTabIds.has(tabId))
      return;
    this._groupTabIds.delete(tabId);
    this._connection.detachTab(tabId);
  }

  private _onTabUpdated(tabId: number, changeInfo: chrome.tabs.TabChangeInfo, tab: chrome.tabs.Tab): void {
    if (changeInfo.groupId !== undefined)
      this._onTabGroupChanged(tabId, tab);
    if (changeInfo.url === undefined)
      return;
    // Chrome resets per-tab badge state on navigation.
    if (this._connection.attachedTabs.has(tabId))
      void this._updateBadge(tabId, CONNECTED_BADGE);
    else if (this._groupTabIds.has(tabId) && !isNonDebuggableUrl(changeInfo.url))
      this._connection.attachTab(tab);
  }

  // One entry point for membership changes, whether the user dragged or we
  // grouped the tab ourselves: attach on entry (if debuggable), detach on
  // exit. A chrome:// tab stays in the group until it navigates somewhere
  // debuggable (handled in _onTabUpdated).
  private _onTabGroupChanged(tabId: number, tab: chrome.tabs.Tab): void {
    const inOurGroup = this._groupId !== null && tab.groupId === this._groupId;
    const wasInGroup = this._groupTabIds.has(tabId);
    if (inOurGroup === wasInGroup)
      return;
    if (inOurGroup) {
      // Chrome may drop another client's still-connecting connect page into
      // our group; that tab is spoken for.
      if (this._isTabReserved(tabId)) {
        void ungroupTabs([tabId]);
        return;
      }
      this._groupTabIds.add(tabId);
      if (!isNonDebuggableUrl(tab.url))
        this._connection.attachTab(tab);
    } else {
      this._groupTabIds.delete(tabId);
      if (this._connection.attachedTabs.has(tabId))
        this._connection.detachTab(tabId);
    }
  }

  private _onTabRemoved(tabId: number): void {
    this._groupTabIds.delete(tabId);
  }

  private _onTabAttached(tabId: number): void {
    void this._updateBadge(tabId, CONNECTED_BADGE);
    void this._addTabToGroup(tabId);
  }

  // Detached (drag-out, close, or an external debugger): clear the badge but
  // leave the tab grouped — the user's intent stands, and a navigation
  // re-attaches via _onTabUpdated.
  private _onTabDetached(tabId: number): void {
    void this._updateBadge(tabId, { text: '' });
  }

  private _onConnectionClose(): void {
    chrome.tabs.onUpdated.removeListener(this._onTabUpdatedListener);
    chrome.tabs.onRemoved.removeListener(this._onTabRemovedListener);
    const groupTabs = [...this._groupTabIds];
    this._groupTabIds.clear();
    if (groupTabs.length)
      void ungroupTabs(groupTabs);
    this.onclose?.();
  }

  private async _updateBadge(tabId: number, { text, color, title }: { text: string; color?: string; title?: string }): Promise<void> {
    try {
      await Promise.all([
        chrome.action.setBadgeText({ tabId, text }),
        chrome.action.setTitle({ tabId, title: title || '' }),
        color ? chrome.action.setBadgeBackgroundColor({ tabId, color }) : Promise.resolve(),
      ]);
    } catch {
      // The tab may be gone already.
    }
  }

  // `_groupTabIds` is updated after the await so an onUpdated event racing
  // it (`_groupId` still null, wasInGroup false) is a no-op rather than a
  // drag-out.
  private async _addTabToGroup(tabId: number): Promise<void> {
    if (this._groupTabIds.has(tabId))
      return;
    try {
      await retryOnDrag(async () => {
        if (this._groupId === null) {
          this._groupId = await chrome.tabs.group({ tabIds: [tabId] });
          await chrome.tabGroups.update(this._groupId, this.groupStyle);
        } else {
          await chrome.tabs.group({ groupId: this._groupId, tabIds: [tabId] });
        }
      });
      this._groupTabIds.add(tabId);
    } catch (error: any) {
      debugLog('Error adding tab to group:', error);
    }
  }
}

export async function ungroupTabs(tabIds: number[]): Promise<void> {
  try {
    await retryOnDrag(() => chrome.tabs.ungroup(tabIds));
  } catch (error: any) {
    debugLog('Error ungrouping tabs:', error);
  }
}

// Chrome throws "user may be dragging a tab" while a drag is in progress.
async function retryOnDrag(fn: () => Promise<void>): Promise<void> {
  const delays = [0, 100, 200, 400, 800];
  let lastError: unknown;
  for (const delay of delays) {
    if (delay)
      await new Promise(resolve => setTimeout(resolve, delay));
    try {
      await fn();
      return;
    } catch (error: any) {
      if (!error?.message?.includes('user may be dragging a tab'))
        throw error;
      lastError = error;
    }
  }
  throw lastError;
}
