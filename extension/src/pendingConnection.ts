// Relay URLs a connect page announced, keyed by that page's tab id. The
// socket opens only when the user clicks Allow — nothing dials out on a
// page load alone.
//
// Derived from packages/extension/src/pendingConnection.ts in
// microsoft/playwright (Apache-2.0, Copyright (c) Microsoft Corporation).

import { RelayConnection, debugLog } from './relayConnection';

const CONNECT_TIMEOUT_MS = 5000;

export class PendingConnections {
  private _map = new Map<number, string>();

  constructor() {
    chrome.tabs.onRemoved.addListener(tabId => this._map.delete(tabId));
  }

  create(selectorTabId: number, relayUrl: string): void {
    this._map.set(selectorTabId, relayUrl);
  }

  // A connect page awaiting approval; no connection may claim its tab.
  has(selectorTabId: number): boolean {
    return this._map.has(selectorTabId);
  }

  async take(selectorTabId: number): Promise<RelayConnection | undefined> {
    const relayUrl = this._map.get(selectorTabId);
    if (relayUrl === undefined)
      return undefined;
    this._map.delete(selectorTabId);
    return openRelayConnection(relayUrl);
  }
}

async function openRelayConnection(relayUrl: string): Promise<RelayConnection> {
  const socket = new WebSocket(relayUrl);
  try {
    await new Promise<void>((resolve, reject) => {
      socket.onopen = () => resolve();
      socket.onerror = () => reject(new Error('WebSocket error'));
      setTimeout(() => reject(new Error('Connection timeout')), CONNECT_TIMEOUT_MS);
    });
    return new RelayConnection(socket);
  } catch (error: any) {
    // Nothing handles this socket any more, and the relay accepts ONE
    // extension link: a socket that completes its handshake after we gave up
    // would hold that slot with no one answering on it.
    socket.close();
    const message = `Failed to connect to the relay: ${error.message}`;
    debugLog(message);
    throw new Error(message);
  }
}
