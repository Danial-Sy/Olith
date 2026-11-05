// SPDX-License-Identifier: AGPL-3.0-or-later
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function panel() {
  const elements = new Map(), timers = new Map();
  let clock = 0, nextTimer = 1, generations = 0;
  let handler = async route => route.endsWith('/status') ? {status: 'ready', expires_in: 120} : {code: `ABCDEF0${++generations}`, expires_in: 120};
  const calls = [];
  const get = selector => {
    if (!elements.has(selector)) {
      const listeners = new Map();
      elements.set(selector, {textContent: '', disabled: false, open: false,
        addEventListener: (event, callback) => listeners.set(event, callback),
        showModal() { this.open = true; }, close() { this.open = false; listeners.get('close')?.(); }});
    }
    return elements.get(selector);
  };
  class Clock extends Date { static now() { return clock; } }
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/integrations.js'), 'utf8'), {
    $: get, Date: Clock,
    api: async (...args) => { calls.push(args); return handler(...args); },
    setInterval: (callback, delay) => { const id = nextTimer++; timers.set(id, {callback, delay}); return id; },
    clearInterval: id => timers.delete(id),
  });
  get('#btnVSCode').onclick();
  return {get, calls, timers, setTime: time => { clock = time; }, setApi: api => { handler = api; },
    async tick(delay) { for (const {callback} of [...timers.values()].filter(t => t.delay === delay)) await callback(); }};
}

test('a pairing code counts down, expires visibly and does not regenerate itself', async () => {
  const ui = panel(); await ui.get('#newIDECode').onclick();
  assert.equal(ui.get('#idePairCode').textContent, 'ABCDEF01');
  assert.match(ui.get('#idePairStatus').textContent, /2:00/);
  ui.setTime(1000); await ui.tick(250);
  assert.match(ui.get('#idePairStatus').textContent, /1:59/);
  ui.setTime(120000); await ui.tick(250);
  assert.equal(ui.get('#idePairCode').textContent, '');
  assert.match(ui.get('#idePairStatus').textContent, /Code expired/);
  assert.equal(ui.get('#newIDECode').textContent, 'Generate new code');
  assert.equal(ui.timers.size, 0);
  assert.equal(ui.calls.length, 1);
});

test('generating another code replaces the previous countdown', async () => {
  const ui = panel(); await ui.get('#newIDECode').onclick();
  ui.setTime(30000); await ui.get('#newIDECode').onclick();
  assert.equal(ui.get('#idePairCode').textContent, 'ABCDEF02');
  assert.equal(ui.timers.size, 2);
  ui.setTime(120000); await ui.tick(250);
  assert.match(ui.get('#idePairStatus').textContent, /0:30/);
});

test('a consumed or replaced code is cleared by server status', async () => {
  const ui = panel(); await ui.get('#newIDECode').onclick();
  ui.setApi(async () => ({status: 'unavailable', expires_in: 0}));
  await ui.tick(2000);
  assert.equal(ui.get('#idePairCode').textContent, '');
  assert.match(ui.get('#idePairStatus').textContent, /used or replaced/);
  assert.equal(ui.timers.size, 0);
});

test('local expiry still works when status requests fail', async () => {
  const ui = panel(); await ui.get('#newIDECode').onclick();
  ui.setApi(async () => { throw new Error('Disconnected'); });
  await ui.tick(2000);
  ui.setTime(120000); await ui.tick(250);
  assert.match(ui.get('#idePairStatus').textContent, /Code expired/);
});

test('closing the dialog stops timers and ignores a late generation response', async () => {
  const ui = panel(); let complete;
  ui.setApi(() => new Promise(resolve => { complete = resolve; }));
  const request = ui.get('#newIDECode').onclick();
  ui.get('#ideDialog').close();
  complete({code: 'ABCDEF12', expires_in: 120}); await request;
  assert.equal(ui.get('#idePairCode').textContent, '');
  assert.equal(ui.get('#newIDECode').disabled, false);
  assert.equal(ui.timers.size, 0);
});

test('a late old status response cannot clear the new code', async () => {
  const ui = panel(); await ui.get('#newIDECode').onclick(); let complete;
  ui.setApi(route => route.endsWith('/status') ? new Promise(resolve => { complete = resolve; }) : Promise.resolve({code: '1234ABCD', expires_in: 120}));
  const status = ui.tick(2000);
  await ui.get('#newIDECode').onclick();
  complete({status: 'unavailable'}); await status;
  assert.equal(ui.get('#idePairCode').textContent, '1234ABCD');
  assert.equal(ui.timers.size, 2);
});

test('a failed generation shows its error and re-enables the button', async () => {
  const ui = panel(); ui.setApi(async () => { throw new Error('Backend unavailable'); });
  await ui.get('#newIDECode').onclick();
  assert.equal(ui.get('#idePairStatus').textContent, 'Backend unavailable');
  assert.equal(ui.get('#idePairCode').textContent, '');
  assert.equal(ui.get('#newIDECode').disabled, false);
});

test('the download action gives saving instructions without preventing native navigation', () => {
  const ui = panel(); let prevented = false;
  ui.get('#downloadIDE').onclick({preventDefault: () => { prevented = true; }});
  assert.match(ui.get('#ideDownloadStatus').textContent, /olith-vscode\.vsix/);
  assert.equal(prevented, false);
});
