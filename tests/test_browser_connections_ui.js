// SPDX-License-Identifier: AGPL-3.0-or-later
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup() {
  const fields = new Map(), calls = [], timers = new Map(), tools = [];
  let timerId = 0;
  function element() {
    let html = '';
    return {value: '', textContent: '', children: [], dataset: {}, disabled: false, hidden: false,
      get innerHTML() {return html;}, set innerHTML(value) {html = value; this.children = [];},
      appendChild(child) {this.children.push(child);},
      replaceChildren(...children) {this.children = children;},
      setAttribute(name, value) {this[name] = value;}, removeAttribute(name) {delete this[name];}};
  }
  const get = id => {if (!fields.has(id)) fields.set(id, element()); return fields.get(id);};
  const vercel = {id: 'vercel', label: 'Vercel', kind: 'session', category: 'Hosting', note: 'Projects and deployments', tool_preset: 'vercel-cli'};
  const data = {connections: [], available: [vercel], keyring: {ok: true}};
  let handler = async (route, options) => {
    if (route === '/api/connections' && !options) return structuredClone(data);
    if (route === '/api/connections') {
      data.connections = [{...vercel, status: 'not connected'}]; data.available = []; return data.connections[0];
    }
    const connection = data.connections[0];
    if (route.endsWith('/sign-in')) {connection.status = 'signing in'; return {...connection, ok: true, next: 'Sign in in Olith’s browser, then return here.'};}
    if (route.endsWith('/status')) {connection.status = 'confirm sign-in'; return {...connection, status_note: 'Confirm your browser session. Service tools connect separately.'};}
    if (route.endsWith('/confirm')) {connection.status = 'connected'; return {...connection, verification: 'user confirmed'};}
    throw new Error(`Unexpected route: ${route}`);
  };
  const context = vm.createContext({$: get, state: {}, document: {createElement: element, hidden: false},
    Option: class {constructor(label, value) {this.label = label; this.value = value;}},
    escapeHtml: value => String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'),
    setStatus() {}, openToolConnection: (...args) => tools.push(args),
    api: async (route, options) => {calls.push([route, options]); return handler(route, options);},
    setTimeout: callback => {const id = ++timerId; timers.set(id, callback); return id;}, clearTimeout: id => timers.delete(id),
  });
  for (const name of ['connection_logos.js', 'browser_connections.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../static', name), 'utf8'), context);
  }
  const actions = row => row.children.find(c => c.className === 'conn-actions').children;
  return {get, calls, timers, context, data, tools, vercel, actions,
    setApi: value => {handler = value;}, refresh: () => context.refreshConnections(),
    poll: async () => {const [id, callback] = timers.entries().next().value; timers.delete(id); await callback();}};
}

test('a website catalog entry offers explicit tool access separately from browser sign-in', async () => {
  const ui = setup(); await ui.refresh();
  const buttons = ui.actions(ui.get('#connAvailable').children[0]);
  assert.deepEqual(buttons.map(b => b.textContent), ['Connect tools', 'Browser sign-in']);
  await buttons[0].onclick();
  assert.equal(ui.tools[0][1], 'vercel-cli');
  assert.equal(ui.calls.filter(([, options]) => options?.method === 'POST').length, 0);
});

test('browser return shows a confirmation automatically without a separate Check click', async () => {
  const ui = setup(); await ui.refresh();
  await ui.actions(ui.get('#connAvailable').children[0])[1].onclick();
  assert.equal(ui.get('#connectionActivity').hidden, false);
  assert.match(ui.get('#connectionActivity').textContent, /Sign in/);
  await ui.poll();
  const buttons = ui.actions(ui.get('#connAvailable').children.find(row => row.dataset.service === 'vercel'));
  assert.ok(buttons.some(b => b.textContent === 'Confirm browser session'));
  assert.equal(ui.calls.some(([route]) => route.endsWith('/check')), false);
  await buttons.find(b => b.textContent === 'Confirm browser session').onclick();
  assert.match(ui.get('#connList').children[0].innerHTML, /Browser session/);
  assert.match(ui.get('#connectionActivity').textContent, /tools connect separately/);
  assert.equal(ui.calls.some(([route]) => route.startsWith('/api/tool-connections')), false);
  assert.equal(ui.timers.size, 0);
});

test('opening shows immediate progress and restores the button after a failure', async () => {
  const ui = setup();
  let resolve;
  ui.setApi(async route => route.endsWith('/sign-in') ? new Promise(done => {resolve = done;}) : structuredClone(ui.data));
  const row = ui.context.connectionRow({...ui.vercel, status: 'not connected'});
  const button = ui.actions(row).find(b => b.textContent === 'Browser sign-in');
  const pending = button.onclick();
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, 'Opening browser…');
  assert.equal(button['aria-busy'], 'true');
  assert.match(ui.get('#connectionActivity').textContent, /Opening Olith/);
  resolve({ok: false, error: 'Browser could not start'}); await pending;
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, 'Browser sign-in');
  assert.equal(ui.get('#connectionActivity').dataset.error, 'true');
  assert.equal(ui.get('#connectionActivity').textContent, 'Browser could not start');
});

test('a late session observation cannot revive a forgotten connection', async () => {
  const ui = setup(); ui.data.connections = [{...ui.vercel, status: 'signing in'}];
  ui.data.available = []; await ui.refresh();
  let resolve;
  ui.setApi(async route => route.endsWith('/status') ? new Promise(done => {resolve = done;}) : structuredClone(ui.data));
  const pending = ui.poll();
  ui.data.connections = []; await ui.refresh();
  resolve({...ui.vercel, status: 'confirm sign-in'}); await pending;
  assert.equal(ui.context.state.connections.connections.length, 0);
  assert.equal(ui.timers.size, 0);
});

test('Check failures are visible and cannot be mistaken for successful sign-in', async () => {
  const ui = setup();
  ui.setApi(async route => route.endsWith('/check') ? {ok: false, error: 'Website did not load'} : structuredClone(ui.data));
  const buttons = ui.actions(ui.context.connectionRow({...ui.vercel, status: 'not connected'}));
  await buttons.find(b => b.textContent === 'Check session').onclick();
  assert.equal(ui.get('#connectionActivity').textContent, 'Website did not load');
  assert.equal(ui.get('#connectionActivity').dataset.error, 'true');
});
