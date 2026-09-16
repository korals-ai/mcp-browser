// One row of the tab list: favicon, title, URL, and an action.
//
// Derived from packages/extension/src/ui/tabItem.tsx in microsoft/playwright
// (Apache-2.0, Copyright (c) Microsoft Corporation); rewritten without React.

export function renderTabItem(tab: chrome.tabs.Tab, action: string, onAction: () => void): HTMLElement {
  const row = document.createElement('div');
  row.className = 'tab-item';
  const icon = document.createElement('img');
  icon.className = 'tab-favicon';
  icon.alt = '';
  if (tab.favIconUrl && !tab.favIconUrl.startsWith('chrome'))
    icon.src = tab.favIconUrl;
  const text = document.createElement('div');
  text.className = 'tab-text';
  const title = document.createElement('div');
  title.className = 'tab-title';
  title.textContent = tab.title || '(untitled)';
  const url = document.createElement('div');
  url.className = 'tab-url';
  url.textContent = tab.url || '';
  text.append(title, url);
  const button = document.createElement('button');
  button.className = 'button';
  button.textContent = action;
  button.addEventListener('click', onAction);
  row.append(icon, text, button);
  return row;
}
