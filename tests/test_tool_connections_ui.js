// SPDX-License-Identifier: AGPL-3.0-or-later
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup({browser = false} = {}) {
  const fields = new Map(), calls = [], timers = new Map();
  let nextTimer = 1;
  function element() {
    const listeners = new Map();
    let html = '';
    return {value: '', checked: true, hidden: false, disabled: false, children: [], open: false, textContent: '', dataset: {},
      get innerHTML() {return html;}, set innerHTML(value) {html = value; this.children = [];},
      appendChild(child) {this.children.push(child);}, setAttribute(name, value) {this[name] = value;},
      append(...nodes) {this.children.push(...nodes);}, replaceChildren(...nodes) {this.children = nodes;},
      removeAttribute(name) {delete this[name];}, addEventListener(name, fn) {listeners.set(name, fn);},
      showModal() {this.open = true;}, close() {this.open = false; listeners.get('close')?.();},
      reportValidity() {return true;},
      reset() {for (const item of fields.values()) {item.value = ''; item.checked = true;} get('#toolConnectionTransport').value = 'http'; get('#toolConnectionAuth').value = 'oauth';},
    };
  }
  const get = id => {if (!fields.has(id)) fields.set(id, element()); return fields.get(id);};
  const data = {connections: [], presets: [{id: 'railway', label: 'Railway', url: 'https://mcp.railway.com', auth: 'oauth', note: 'Railway tools', docs: 'https://docs.railway.com/ai/mcp-server'}]};
  for (const id of ['vercel', 'netlify']) data.presets.push({id: id + '-cli', provider: id, label: id + ' CLI', transport: 'cli', auth: 'none', docs: 'https://example.invalid/cli', install: `npm install -g ${id}`, login: `${id} login`});
  let handler = async route => route === '/api/tool-connections' ? data : {status: 'connected', tool_count: 2, busy: false};
  const context = vm.createContext({$: get, URL, state: {}, setStatus() {}, location: {href: 'http://127.0.0.1:7878/'},
    escapeHtml: value => String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'),
    document: {createElement: element}, Option: class {constructor(label, value) {this.label = label; this.value = value;}},
    connButton: (label, action) => ({label, onclick: action}), confirmAction: async () => true,
    api: async (route, options) => {calls.push([route, options]); return handler(route, options);},
    setTimeout: callback => {const id = nextTimer++; timers.set(id, callback); return id;}, clearTimeout: id => timers.delete(id),
  });
  for (const script of ['connection_logos.js', ...(browser ? ['browser_connections.js'] : []), 'tool_connections.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../static', script), 'utf8'), context);
  }
  return {get, calls, data, timers, context, setApi: value => {handler = value;},
    open: (entry, presetId) => context.openToolConnection(entry, presetId), submit: () => get('#toolConnectionForm').onsubmit({preventDefault() {}, target: get('#toolConnectionForm')})};
}

test('opening setup only reads local metadata and Railway fills the official setup', async () => {
  const ui = setup(); await ui.open();
  assert.ok(ui.calls.every(([, options]) => !options));
  ui.get('#toolConnectionPreset').value = 'railway'; ui.get('#toolConnectionPreset').onchange();
  assert.equal(ui.get('#toolConnectionURL').value, 'https://mcp.railway.com');
  assert.equal(ui.get('#toolConnectionAuth').value, 'oauth');
  assert.match(ui.get('#toolConnectionPresetIcon').innerHTML, /\/railway\.svg/);
  assert.match(ui.get('#toolConnectionCallback').textContent, /7878\/api\/tool-connections\/railway\/callback/);
  ui.get('#toolConnectionRailwayCLI').onclick();
  assert.equal(ui.get('#toolConnectionCommand').value, 'railway');
  assert.equal(ui.get('#toolConnectionArgs').value, '["mcp"]');
  assert.equal(ui.get('#toolConnectionURL').disabled, true);
  assert.equal(ui.get('#toolConnectionCommand').required, true);
});

test('saved tool cards show their service logos and custom servers use the fallback', async () => {
  const ui = setup();
  ui.data.connections.push(
    {id: 'railway', label: 'Railway', status: 'connected', enabled: true, tool_count: 2},
    {id: 'private-tools', label: 'Private tools', status: 'not tested', enabled: false, tool_count: 0},
  );
  await ui.context.refreshToolConnections();
  const rows = ui.get('#toolConnectionList').children;
  assert.match(rows[0].children[0].children[0].innerHTML, /\/railway\.svg/);
  assert.equal(rows[0].children[0].children[1].textContent, 'Railway');
  assert.match(rows[1].children[0].children[0].innerHTML, /\/custom\.svg/);
  assert.match(rows[1].children[1].textContent, /Paused/);
});

test('switching from a preset to a custom server resets the logo', async () => {
  const ui = setup(); await ui.open();
  ui.get('#toolConnectionPreset').value = 'railway'; ui.get('#toolConnectionPreset').onchange();
  assert.match(ui.get('#toolConnectionPresetIcon').innerHTML, /\/railway\.svg/);
  ui.get('#toolConnectionPreset').value = ''; ui.get('#toolConnectionPreset').onchange();
  assert.match(ui.get('#toolConnectionPresetIcon').innerHTML, /\/custom\.svg/);
});

test('the catalog opens the chosen tools preset and manages an existing connection', async () => {
  const ui = setup(); await ui.open(null, 'railway');
  assert.equal(ui.get('#toolConnectionPreset').value, 'railway');
  assert.equal(ui.get('#toolConnectionURL').value, 'https://mcp.railway.com');
  ui.data.connections.push({id: 'railway', label: 'My Railway', url: 'https://mcp.railway.com', transport: 'http', auth: 'oauth', enabled: true});
  await ui.open(null, 'railway');
  assert.equal(ui.get('#toolConnectionLabel').value, 'My Railway');
  assert.equal(ui.get('#toolConnectionId').disabled, true);
});

test('CLI setup uses the existing terminal login and hides OAuth and MCP configuration', async () => {
  const ui = setup(); await ui.open(null, 'vercel-cli');
  assert.equal(ui.get('#toolConnectionTransport').value, 'cli');
  assert.equal(ui.get('#toolConnectionProvider').value, 'vercel');
  assert.equal(ui.get('#toolConnectionCLI').hidden, false);
  assert.equal(ui.get('#toolConnectionProviderField').hidden, true);
  assert.equal(ui.get('#toolConnectionHTTP').hidden, true);
  assert.equal(ui.get('#toolConnectionLocal').hidden, true);
  assert.equal(ui.get('#toolConnectionURL').disabled, true);
  assert.equal(ui.get('#toolConnectionCommand').disabled, true);
  assert.equal(ui.get('#toolConnectionSettings').open, false);
  assert.equal(ui.get('#toolConnectionSave').textContent, 'Verify CLI connection');
  assert.match(ui.get('#toolConnectionCLISetup').textContent, /npm install -g vercel\nvercel login/);
  ui.get('#toolConnectionToken').value = 'stale-secret';
  await ui.submit();
  const save = ui.calls.find(([route, options]) => route === '/api/tool-connections' && options?.method === 'POST');
  const body = JSON.parse(save[1].body);
  assert.equal(body.transport, 'cli'); assert.equal(body.provider, 'vercel');
  assert.equal(body.auth, 'none'); assert.equal(body.token, null); assert.equal(body.url, '');
  assert.equal(ui.calls.some(([, options]) => options?.body?.includes('stale-secret')), false);
});

test('changing the CLI provider keeps its service name, ID, setup and logo consistent', async () => {
  const ui = setup(); await ui.open(null, 'vercel-cli');
  ui.get('#toolConnectionProvider').value = 'netlify'; ui.get('#toolConnectionProvider').onchange();
  assert.equal(ui.get('#toolConnectionPreset').value, 'netlify-cli');
  assert.equal(ui.get('#toolConnectionId').value, 'netlify-cli');
  assert.match(ui.get('#toolConnectionCLISetup').textContent, /netlify login/);
  assert.match(ui.get('#toolConnectionPresetIcon').innerHTML, /\/netlify\.svg/);
});

test('Linux permissions guidance follows npm presets without elevating the copied login commands', async () => {
  const ui = setup(); await ui.open(null, 'vercel-cli');
  assert.equal(ui.get('#toolConnectionLinuxHelp').hidden, false);
  assert.equal(ui.get('#toolConnectionSudoInstall').textContent, 'sudo npm install -g vercel');
  assert.equal(ui.get('#toolConnectionCLISetup').textContent, 'npm install -g vercel\nvercel login');
  ui.get('#toolConnectionProvider').value = 'netlify'; ui.get('#toolConnectionProvider').onchange();
  assert.equal(ui.get('#toolConnectionSudoInstall').textContent, 'sudo npm install -g netlify');
  ui.data.presets.push({id: 'kaggle-cli', provider: 'kaggle', transport: 'cli', auth: 'none', install: 'python -m pip install kaggle', login: 'kaggle auth login'});
  ui.get('#toolConnectionPreset').value = 'kaggle-cli'; ui.get('#toolConnectionPreset').onchange();
  assert.equal(ui.get('#toolConnectionLinuxHelp').hidden, true);
  assert.equal(ui.get('#toolConnectionSudoInstall').textContent, '');
  assert.doesNotMatch(ui.get('#toolConnectionCLISetup').textContent, /sudo/);
});

test('badges distinguish verified CLI, paused CLI, unverified CLI and MCP access', async () => {
  const ui = setup();
  const cli = {id: 'work-vercel', provider: 'vercel', transport: 'cli', label: 'Work Vercel', status: 'connected', enabled: true, tool_count: 2};
  ui.data.connections.push(cli);
  await ui.context.refreshToolConnections();
  assert.match(ui.get('#toolConnectionList').children[0].children[0].children[2].innerHTML, /ready.*CLI tools/);
  assert.match(ui.context.serviceToolBadges({id: 'vercel'}), /CLI tools/);
  assert.equal(ui.context.serviceToolBadges({id: 'netlify'}), '');
  cli.enabled = false;
  await ui.context.refreshToolConnections();
  assert.match(ui.context.serviceToolBadges({id: 'vercel'}), /CLI paused/);
  cli.enabled = true; cli.status = 'login required';
  await ui.context.refreshToolConnections();
  assert.match(ui.context.serviceToolBadges({id: 'vercel'}), /CLI not ready/);
  assert.doesNotMatch(ui.context.serviceToolBadges({id: 'vercel'}), /class="[^"]*ready/);
  ui.data.connections.push({...cli, id: 'working-vercel', status: 'connected'});
  assert.match(ui.context.serviceToolBadges({id: 'vercel'}), /CLI tools/);
  assert.doesNotMatch(ui.context.serviceToolBadges({id: 'vercel'}), /not ready/);
  ui.data.connections.push({id: 'custom-railway', transport: 'http', url: 'https://mcp.railway.com', status: 'connected', enabled: true});
  assert.match(ui.context.serviceToolBadges({id: 'railway', tool_preset: 'railway-cli'}), /MCP tools/);
});

test('CLI-only services move between groups after verify, pause, enable and forget', async () => {
  const ui = setup({browser: true});
  const service = {id: 'vercel', label: 'Vercel', kind: 'session', tool_preset: 'vercel-cli'};
  const websites = {connections: [], available: [service], keyring: {ok: true}};
  const cli = {id: 'vercel-cli', provider: 'vercel', label: 'Vercel CLI', transport: 'cli', status: 'connected', enabled: true, tool_count: 2};
  let browserFails = false;
  ui.setApi(async (route, options) => {
    if (route === '/api/connections') {if (browserFails) throw new Error('Browser unavailable'); return websites;}
    if (options?.method === 'PATCH') {cli.enabled = JSON.parse(options.body).enabled; return cli;}
    if (options?.method === 'DELETE') {ui.data.connections = []; return {};}
    return ui.data;
  });
  const find = group => ui.get(group).children.find(row => row.dataset.service === 'vercel');
  const toolRow = row => row.children.find(child => child.className?.startsWith('conn-row'));
  await ui.context.refreshConnections();
  assert.ok(find('#connAvailable')); assert.equal(find('#connList'), undefined);
  ui.data.connections.push(cli);
  await ui.context.refreshToolConnections();
  assert.ok(find('#connList')); assert.equal(find('#connAvailable'), undefined);
  assert.match(find('#connList').innerHTML, /CLI tools/);
  // The website is still disconnected: only the CLI made this service live.
  assert.equal(websites.connections.length, 0);
  await toolRow(find('#connList')).children[2].children[1].onclick();
  assert.equal(find('#connList'), undefined); assert.ok(find('#connAvailable'));
  await toolRow(find('#connAvailable')).children[2].children[1].onclick();
  assert.ok(find('#connList'));
  await toolRow(find('#connList')).children[2].children[2].onclick();
  assert.equal(find('#connList'), undefined); assert.ok(find('#connAvailable'));
  browserFails = true; ui.data.connections.push(cli);
  await ui.context.refreshConnections();
  assert.ok(find('#connList'), 'browser failure must not block CLI status');
  websites.connections = [{...service, status: 'connected'}]; websites.available = [];
  browserFails = false; cli.enabled = false;
  await ui.context.refreshConnections();
  assert.ok(find('#connList'), 'a live browser session still counts when tools are paused');
  assert.match(find('#connList').innerHTML, /CLI paused.*Browser session/);
});

test('custom tool-only connections work without browser metadata and respect search', async () => {
  const ui = setup({browser: true});
  ui.data.connections.push({id: 'private-tools', label: 'Private tools', transport: 'http', status: 'connected', enabled: true, tool_count: 2});
  await ui.context.refreshToolConnections();
  const find = () => ui.get('#connList').children.find(row => row.dataset.service === 'private-tools');
  assert.ok(find());
  ui.get('#connectionSearch').value = 'unrelated'; ui.context.renderConnectionCatalog();
  assert.equal(find().hidden, true);
  ui.get('#connectionSearch').value = 'private'; ui.context.renderConnectionCatalog();
  assert.equal(find().hidden, false);
});

test('a working MCP connection is preserved when a service gains a preferred CLI option', async () => {
  const ui = setup();
  ui.data.presets.push({id: 'vercel', label: 'Vercel', url: 'https://mcp.vercel.com', auth: 'oauth', requires_client: true});
  ui.data.connections.push({id: 'vercel', label: 'My Vercel tools', url: 'https://mcp.vercel.com', transport: 'http', auth: 'oauth', enabled: true, status: 'connected', has_oauth: true});
  await ui.open(null, 'vercel-cli');
  assert.equal(ui.get('#toolConnectionLabel').value, 'My Vercel tools');
  assert.equal(ui.get('#toolConnectionTransport').value, 'http');
  await ui.submit();
  assert.ok(ui.calls.some(([, options]) => options?.method === 'POST'));
});

test('Vercel client requirements stop an unconfigured OAuth attempt before any save', async () => {
  const ui = setup();
  ui.data.presets.push({id: 'vercel', label: 'Vercel', url: 'https://mcp.vercel.com', auth: 'oauth', requires_client: true, note: 'Vercel requires an approved client or HTTPS metadata URL.'});
  await ui.open(null, 'vercel');
  assert.equal(ui.get('#toolConnectionSettings').open, true);
  assert.equal(ui.get('#toolConnectionOAuth').open, true);
  await ui.submit();
  assert.match(ui.get('#toolConnectionStatus').textContent, /requires an OAuth client ID or HTTPS client metadata URL/);
  assert.equal(ui.calls.filter(([, options]) => options?.method === 'POST').length, 0);
  assert.equal(ui.get('#toolConnectionSave').disabled, false);
  ui.get('#toolConnectionClientId').value = 'https://app.example/olith-client.json';
  await ui.submit();
  const save = ui.calls.find(([route, options]) => route === '/api/tool-connections' && options?.method === 'POST');
  assert.equal(JSON.parse(save[1].body).client_id, 'https://app.example/olith-client.json');
});

test('saving a token sends it once, clears secret fields and starts discovery', async () => {
  const ui = setup(); await ui.open();
  ui.get('#toolConnectionLabel').value = 'Example'; ui.get('#toolConnectionLabel').oninput();
  ui.get('#toolConnectionAuth').value = 'bearer'; ui.get('#toolConnectionAuth').onchange();
  ui.get('#toolConnectionURL').value = 'https://example.invalid/mcp';
  ui.get('#toolConnectionToken').value = 'private-token';
  await ui.submit();
  const save = ui.calls.find(([route, options]) => route === '/api/tool-connections' && options?.method === 'POST');
  assert.equal(JSON.parse(save[1].body).token, 'private-token');
  assert.equal(ui.calls.filter(([, options]) => options?.body?.includes('private-token')).length, 1);
  assert.equal(ui.get('#toolConnectionToken').value, '');
  assert.ok(ui.calls.some(([route]) => route === '/api/tool-connections/example/connect'));
});

test('invalid command arguments are caught before any save and the button recovers', async () => {
  const ui = setup(); await ui.open();
  ui.get('#toolConnectionTransport').value = 'stdio'; ui.get('#toolConnectionTransport').onchange();
  ui.get('#toolConnectionArgs').value = 'not json';
  await ui.submit();
  assert.equal(ui.calls.filter(([, options]) => options?.method === 'POST').length, 0);
  assert.match(ui.get('#toolConnectionStatus').textContent, /JSON/);
  assert.equal(ui.get('#toolConnectionSave').disabled, false);
});

test('OAuth redirects reject executable URLs and accept the provider HTTPS link', async () => {
  const ui = setup(); await ui.open();
  ui.context.displayToolConnectionStatus({status: 'signing in', authorization_url: 'javascript:alert(1)', busy: true});
  assert.equal(ui.get('#toolConnectionAuthorize').hidden, true);
  ui.context.displayToolConnectionStatus({status: 'signing in', authorization_url: 'https://provider.example/authorize?state=test', busy: true});
  assert.equal(ui.get('#toolConnectionAuthorize').hidden, false);
  assert.match(ui.get('#toolConnectionAuthorize').href, /^https:/);
});

test('closing setup clears credentials and late status cannot modify the next dialog', async () => {
  const ui = setup(); await ui.open();
  ui.get('#toolConnectionToken').value = 'private-token'; ui.get('#toolConnectionEnv').value = '{"KEY":"secret"}';
  let resolve;
  ui.setApi(async route => route.endsWith('/status') ? new Promise(done => {resolve = done;}) : ui.data);
  const pending = ui.context.watchToolConnection('old', vm.runInContext('toolConnectionEpoch', ui.context));
  ui.get('#toolConnectionClose').onclick();
  assert.equal(ui.get('#toolConnectionToken').value, '');
  assert.equal(ui.get('#toolConnectionEnv').value, '');
  await ui.open();
  resolve({status: 'signing in', authorization_url: 'https://old.example/auth', busy: true});
  await pending;
  assert.equal(ui.get('#toolConnectionAuthorize').hidden, true);
  assert.equal(ui.timers.size, 0);
});
