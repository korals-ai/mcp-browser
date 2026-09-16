// The status page (toolbar icon): who is connected, to which tabs, with a
// Disconnect per client, and the pairing token.
//
// Derived from packages/extension/src/ui/status.tsx in microsoft/playwright
// (Apache-2.0, Copyright (c) Microsoft Corporation); rewritten without React.

import { renderAuthTokenSection } from './authToken';
import { renderTabItem } from './tabItem';

type ConnectionStatus = { id: number; clientName?: string; connectedTabIds: number[] };

const listEl = document.getElementById('connections')!;
const tokenEl = document.getElementById('token')!;

async function loadStatus(): Promise<void> {
  const response = await chrome.runtime.sendMessage({ type: 'getConnectionStatus' });
  const statuses = (response?.connections ?? []) as ConnectionStatus[];
  listEl.innerHTML = '';
  if (statuses.length === 0) {
    const banner = document.createElement('div');
    banner.className = 'status status-info';
    banner.textContent = 'No client is connected. The browser tool opens a connect page here when an agent asks for your browser.';
    listEl.appendChild(banner);
    return;
  }
  for (const status of statuses) {
    const tabs = (await Promise.all(status.connectedTabIds.map(id => chrome.tabs.get(id).catch(() => undefined))))
        .filter((tab): tab is chrome.tabs.Tab => !!tab);
    const section = document.createElement('div');
    section.className = 'connection';
    const header = document.createElement('div');
    header.className = 'connection-header';
    const label = document.createElement('div');
    label.innerHTML = `Connected to <strong></strong>`;
    label.querySelector('strong')!.textContent = `"${status.clientName || 'unknown'}"`;
    const disconnect = document.createElement('button');
    disconnect.className = 'button button-primary';
    disconnect.textContent = 'Disconnect';
    disconnect.addEventListener('click', async () => {
      await chrome.runtime.sendMessage({ type: 'disconnect', connectionId: status.id });
      await loadStatus();
    });
    header.append(label, disconnect);
    const caption = document.createElement('div');
    caption.className = 'section-title';
    caption.textContent = tabs.length === 1 ? 'Accessible page:' : 'Accessible pages:';
    section.append(header, caption);
    for (const tab of tabs) {
      section.appendChild(renderTabItem(tab, 'Open', async () => {
        await chrome.tabs.update(tab.id!, { active: true });
        window.close();
      }));
    }
    listEl.appendChild(section);
  }
}

renderAuthTokenSection(tokenEl);
void loadStatus();
