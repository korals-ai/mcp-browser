// The wire contract, driven with a fake chrome.* and a fake socket:
// the command allow-list, attach/detach bookkeeping, event filtering, the
// last-tab close, downloads and the keepalive. `npm test` builds
// build/test/ first.

import assert from 'node:assert/strict';
import { beforeEach, describe, it } from 'node:test';

function fakeEvent() {
  const listeners = new Set();
  return {
    addListener: l => listeners.add(l),
    removeListener: l => listeners.delete(l),
    emit: (...args) => { for (const l of [...listeners]) l(...args); },
    get size() { return listeners.size; },
  };
}

function fakeChrome() {
  const calls = [];
  const record = name => async (...args) => { calls.push([name, ...args]); return { called: name }; };
  return {
    calls,
    debugger: { attach: record('debugger.attach'), detach: record('debugger.detach'), sendCommand: record('debugger.sendCommand'), onEvent: fakeEvent(), onDetach: fakeEvent() },
    tabs: { create: record('tabs.create'), remove: record('tabs.remove'), get: async id => ({ id }), onCreated: fakeEvent(), onRemoved: fakeEvent() },
    downloads: { search: record('downloads.search'), onCreated: fakeEvent(), onChanged: fakeEvent() },
    runtime: { getManifest: () => ({ version: '0.1.0' }) },
  };
}

function fakeSocket() {
  return { readyState: 1, sent: [], closed: null, send(s) { this.sent.push(JSON.parse(s)); }, close(code, reason) { this.closed = { code, reason }; } };
}

globalThis.WebSocket = { OPEN: 1 };
globalThis.chrome = fakeChrome();
const { RelayConnection } = await import('../build/test/relayConnection.js');
const { uniqueGroupStyle, isNonDebuggableUrl } = await import('../build/test/connectedTabGroup.js');
const { ALLOWED_CHROME_COMMANDS, KEEPALIVE_METHOD } = await import('../build/test/protocol.js');

// What the tab group does for a tab the user hands over: announce it, then
// let the relay attach. A tab that skipped this is not the connection's.
async function offerAndAttach(conn, tabId) {
  conn.attachTab({ id: tabId });
  return conn.handleCommand('chrome.debugger.attach', [{ tabId }, '1.3']);
}

describe('RelayConnection', () => {
  let ws;
  let conn;
  beforeEach(() => {
    globalThis.chrome = fakeChrome();
    ws = fakeSocket();
    conn = new RelayConnection(ws);
  });

  it('refuses any chrome.* call outside the allow-list', async () => {
    await assert.rejects(conn.handleCommand('chrome.tabs.query', [{}]), /Unknown method: chrome.tabs.query/);
    await assert.rejects(conn.handleCommand('chrome.cookies.getAll', [{}]), /Unknown method/);
    assert.equal(chrome.calls.length, 0);
  });

  it('attaches a tab through the allow-list and books it', async () => {
    const attached = [];
    conn.ontabattached = id => attached.push(id);
    const result = await offerAndAttach(conn, 7);
    assert.deepEqual(result, { called: 'debugger.attach' });
    assert.deepEqual(attached, [7]);
    assert.ok(conn.attachedTabs.has(7));
  });

  it('forwards debugger events only for attached tabs', async () => {
    await offerAndAttach(conn, 7);
    chrome.debugger.onEvent.emit({ tabId: 7 }, 'Page.loadEventFired', {});
    chrome.debugger.onEvent.emit({ tabId: 8 }, 'Page.loadEventFired', {});
    const forwarded = ws.sent.filter(m => m.method === 'chrome.debugger.onEvent');
    assert.equal(forwarded.length, 1);
    assert.equal(forwarded[0].params[0].tabId, 7);
  });

  it('forwards a popup only when an attached tab opened it', async () => {
    await offerAndAttach(conn, 7);
    chrome.tabs.onCreated.emit({ id: 20, openerTabId: 7 });
    chrome.tabs.onCreated.emit({ id: 21, openerTabId: 9 });
    chrome.tabs.onCreated.emit({ id: 22 });
    const created = ws.sent.filter(m => m.method === 'chrome.tabs.onCreated');
    // 7 is the hand-over announcement; 20 the popup it opened. 21 and 22 were
    // opened by tabs this connection does not have.
    assert.deepEqual(created.map(m => m.params[0].id), [7, 20]);
  });

  it('closes with "All controlled tabs detached" when the last tab leaves', async () => {
    await offerAndAttach(conn, 7);
    let closed = 0;
    conn.onclose = () => closed++;
    conn.detachTab(7);
    const detach = ws.sent.find(m => m.method === 'chrome.debugger.onDetach');
    assert.deepEqual(detach.params, [{ tabId: 7 }, 'target_closed']);
    assert.deepEqual(ws.closed, { code: 1000, reason: 'All controlled tabs detached' });
    assert.equal(closed, 1);
    assert.ok(conn.closed);
  });

  it('a tab Chrome detached with target_closed is retried, then given up', async () => {
    await offerAndAttach(conn, 7);
    chrome.tabs.get = async () => { throw new Error('No tab with id: 7.'); };
    chrome.debugger.onDetach.emit({ tabId: 7 }, 'target_closed');
    assert.equal(ws.closed, null); // not yet: the re-attach window is open
    await new Promise(r => setTimeout(r, 250));
    assert.deepEqual(ws.closed, { code: 1000, reason: 'All controlled tabs detached' });
  });

  it('forwards download events whole and allows downloads.search', async () => {
    chrome.downloads.onCreated.emit({ id: 3, url: 'https://x/f.pdf', state: 'in_progress' });
    chrome.downloads.onChanged.emit({ id: 3, state: { current: 'complete', previous: 'in_progress' } });
    const events = ws.sent.filter(m => m.method?.startsWith('chrome.downloads.'));
    assert.deepEqual(events.map(m => m.method), ['chrome.downloads.onCreated', 'chrome.downloads.onChanged']);
    await conn.handleCommand('chrome.downloads.search', [{ orderBy: ['-startTime'], limit: 5 }]);
    assert.deepEqual(chrome.calls.at(-1), ['downloads.search', { orderBy: ['-startTime'], limit: 5 }]);
    assert.ok(ALLOWED_CHROME_COMMANDS.has('chrome.downloads.search'));
  });

  it('refuses every tab-scoped command for a tab the user never handed over', async () => {
    await offerAndAttach(conn, 7);
    for (const [method, args] of [
      ['chrome.debugger.attach', [{ tabId: 8 }, '1.3']],
      ['chrome.debugger.sendCommand', [{ tabId: 8 }, 'Page.navigate', { url: 'https://evil' }]],
      ['chrome.debugger.detach', [{ tabId: 8 }]],
      ['chrome.tabs.remove', [8]],
    ])
      await assert.rejects(conn.handleCommand(method, args), /Tab 8 was not handed to this connection/);
    // Nothing reached chrome.*: the refusal is the extension's, not the relay's.
    assert.deepEqual(chrome.calls.map(c => c[0]), ['debugger.attach']);
    assert.ok(!conn.attachedTabs.has(8));
  });

  it('a tab that leaves the group can no longer be driven', async () => {
    await offerAndAttach(conn, 7);
    await offerAndAttach(conn, 9);
    conn.detachTab(7);
    await assert.rejects(
        conn.handleCommand('chrome.debugger.sendCommand', [{ tabId: 7 }, 'Page.reload', {}]),
        /Tab 7 was not handed to this connection/);
    await conn.handleCommand('chrome.debugger.sendCommand', [{ tabId: 9 }, 'Page.reload', {}]);
  });

  it('a tab the client opened, and a popup an attached tab opened, are its own', async () => {
    await offerAndAttach(conn, 7);
    chrome.tabs.create = async () => ({ id: 30 });
    await conn.handleCommand('chrome.tabs.create', [{ url: 'https://example.com' }]);
    await conn.handleCommand('chrome.debugger.attach', [{ tabId: 30 }, '1.3']);
    assert.ok(conn.attachedTabs.has(30));
    chrome.tabs.onCreated.emit({ id: 31, openerTabId: 7 });
    await conn.handleCommand('chrome.debugger.attach', [{ tabId: 31 }, '1.3']);
    assert.ok(conn.attachedTabs.has(31));
  });

  it('a failing command is logged without its params', async () => {
    const logged = [];
    const realLog = console.log;
    console.log = (...args) => logged.push(args.join(' '));
    try {
      chrome.debugger.sendCommand = async () => { throw new Error('Detached'); };
      await offerAndAttach(conn, 7);
      conn._onMessage({ data: JSON.stringify({ id: 4, method: 'chrome.debugger.sendCommand', params: [{ tabId: 7 }, 'Input.insertText', { text: 'hunter2' }] }) });
      await new Promise(r => setTimeout(r, 10));
    } finally {
      console.log = realLog;
    }
    assert.ok(logged.some(l => l.includes('chrome.debugger.sendCommand') && l.includes('id 4')));
    assert.ok(!logged.some(l => l.includes('hunter2')), logged.join('\n'));
    assert.equal(ws.sent.at(-1).error, 'Detached');
  });

  it('keepalive is a method the relay can ignore', () => {
    conn.keepalive();
    assert.deepEqual(ws.sent.at(-1), { method: KEEPALIVE_METHOD, params: [] });
  });

  it('closing removes every chrome listener', async () => {
    await offerAndAttach(conn, 7);
    conn.close('User disconnected');
    for (const ev of [chrome.debugger.onEvent, chrome.debugger.onDetach, chrome.tabs.onCreated, chrome.tabs.onRemoved, chrome.downloads.onCreated, chrome.downloads.onChanged])
      assert.equal(ev.size, 0);
    assert.deepEqual(chrome.calls.at(-1), ['debugger.detach', { tabId: 7 }]);
  });
});

describe('tab groups', () => {
  it('names groups per client and never reuses a live title or colour', () => {
    const first = uniqueGroupStyle('browser tool', []);
    assert.deepEqual(first, { title: 'Agent · browser tool', color: 'green' });
    const second = uniqueGroupStyle('browser tool', [first]);
    assert.deepEqual(second, { title: 'Agent · browser tool (2)', color: 'blue' });
    assert.equal(uniqueGroupStyle(undefined, []).title, 'Agent · unknown');
  });

  it('knows which URLs cannot take a debugger', () => {
    assert.ok(isNonDebuggableUrl('chrome://extensions'));
    assert.ok(isNonDebuggableUrl('devtools://devtools/bundled/inspector.html'));
    assert.ok(!isNonDebuggableUrl('https://example.com'));
    assert.ok(!isNonDebuggableUrl(undefined));
  });
});
