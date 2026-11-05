// SPDX-License-Identifier: AGPL-3.0-or-later
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {execFileSync} = require('node:child_process');
const root = path.join(__dirname, '..');
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(root, 'static/connection_logos.js'), 'utf8'), context);
const logos = vm.runInContext('CONNECTION_LOGOS', context);

test('every catalog provider and tool preset has a real, local logo', () => {
  const catalog = JSON.parse(execFileSync(process.env.PYTHON || 'python3', ['-c',
    'import json; from connection_catalog import WEBSITES, MCP_PRESETS, CLI_PRESETS; print(json.dumps(["google", "microsoft", "github"] + [r[0] for r in WEBSITES] + [p["id"] for p in MCP_PRESETS + CLI_PRESETS]))'], {cwd: root, encoding: 'utf8'}));
  for (const id of catalog) {
    const brand = id.replace(/-cli$/, '');
    assert.ok(Object.hasOwn(logos, brand), `Missing logo: ${id}`);
    assert.match(context.connectionIcon({id}), new RegExp(`/${logos[brand].replace('.', '\\.')}"`));
  }
  for (const asset of [...new Set(Object.values(logos)), 'custom.svg']) {
    assert.match(asset, /^[a-z0-9-]+\.(svg|png)$/);
    const bytes = fs.readFileSync(path.join(root, 'static/assets/connections', asset));
    assert.ok(bytes.length > 100, `Empty asset: ${asset}`);
    if (asset.endsWith('.png')) assert.equal(bytes.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
    else {
      assert.match(bytes.toString(), /<svg\b/);
      assert.match(bytes.toString(), /viewBox=/);
      assert.doesNotMatch(bytes.toString(), /<script\b|<foreignObject\b|\son\w+=|(?:href|src)=["'](?:https?:|\/\/|data:)/i);
    }
  }
});

test('unknown, inherited and hostile IDs use the decorative custom logo safely', () => {
  for (const id of ['', 'custom-service', '__proto__', 'constructor', '../../secret', '"><script>bad</script>']) {
    const html = context.connectionIcon({id});
    assert.match(html, /\/custom\.svg"/);
    assert.match(html, /alt=""/);
    assert.doesNotMatch(html, /<script|secret/);
  }
});

test('the shared logo renderer loads before both connection interfaces', () => {
  const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
  const shared = html.indexOf('src="/static/connection_logos.js"');
  assert.ok(shared >= 0 && shared < html.indexOf('src="/static/app.js"'));
  assert.ok(shared < html.indexOf('src="/static/tool_connections.js"'));
});
