// SPDX-License-Identifier: AGPL-3.0-or-later
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {renderMarkdown} = require('../media/markdown');
const {webviewHtml} = require('../webview');

test('replies render headings, lists, inline code, and copyable fenced source', () => {
  const html = renderMarkdown('# Plan\n\n**Check** `app.py`\n\n1. Read\n2. Verify\n\n```python\nprint("hello")\n```');
  assert.match(html, /<h2>Plan<\/h2>/);
  assert.match(html, /<strong>Check<\/strong> <code>app.py<\/code>/);
  assert.match(html, /<ol><li>Read<\/li><li>Verify<\/li><\/ol>/);
  assert.match(html, /aria-label="Copy code"/);
  assert.match(html, /<pre><code>print\(&quot;hello&quot;\)<\/code><\/pre>/);
});

test('model text cannot insert HTML, scripts, images, or executable links', () => {
  for (const text of ['<img src=x onerror="alert(1)">', '**<svg onload=alert(1)>**', '`<script>alert(1)</script>`', '[run](javascript:alert(1))', '```"><img src=x>\n</code><script>alert(1)</script>\n```']) {
    const html = renderMarkdown(text);
    assert.doesNotMatch(html, /<(?:img|svg|script|a)\b|<[^>]+\s(?:href|src|onerror|onload)=/i);
    assert(!html.includes('</code><script>'));
  }
});

test('streaming incomplete fences and nested fence characters preserve source', () => {
  assert.match(renderMarkdown('```js\nconst n = 1;'), /<pre><code>const n = 1;<\/code>/);
  assert.match(renderMarkdown('````md\n```js\na()\n```\n````'), /<pre><code>```js\na\(\)\n```<\/code>/);
  assert.match(renderMarkdown('~~~txt\nx < y\n~~~'), /x &lt; y/);
});

test('webview uses local assets, a nonce CSP, labeled controls, and approved artwork', () => {
  const html = webviewHtml({nonce: 'test-nonce', source: 'https://local.test', asset: name => 'https://local.test/' + name});
  assert.match(html, /default-src 'none'/);
  assert.match(html, /script-src 'nonce-test-nonce'/);
  assert.doesNotMatch(html, /unsafe-inline|unsafe-eval|https:\/\/cdn/);
  for (const asset of ['chat.css', 'chat.js', 'markdown.js', 'olith.svg']) assert(html.includes('https://local.test/' + asset));
  for (const label of ['New chat', 'Review task changes', 'Connect to Olith', 'Send message']) assert(html.includes(`aria-label="${label}"`));
  assert.deepEqual(fs.readFileSync(path.join(__dirname, '../media/olith.svg')), fs.readFileSync(path.join(__dirname, '../../../static/assets/olith-mark.svg')));
});

function host(overrides = {}, ClientClass) {
  const editor = {window: {}, workspace: {textDocuments: []}, ...overrides};
  const sandbox = {module: {exports: {}}, require: name => name === 'vscode' ? editor : name === './client' && ClientClass ? {...require('../client'), Client: ClientClass} : require(name.startsWith('./') ? path.join(__dirname, '..', name) : name), Buffer, AbortController, TextDecoder, setInterval, clearInterval};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../extension.js'), 'utf8'), sandbox);
  const agent = Object.create(sandbox.module.exports.Olith.prototype);
  const updates = [];
  Object.assign(agent, {messages: [], tasks: [], transcript: '', cid: 'chat', running: false, modelKey: 'coder:small', models: [{id: 'coder:small', label: 'Small coder'}], refreshModels: async () => {}, view: {webview: {postMessage: m => updates.push(JSON.parse(JSON.stringify(m)))}}, ensure: async () => {}, client: {session: {root: '/project'}, stream: async () => {}}});
  return {agent, updates, editor};
}

test('live commentary and tool activity arrive before the final reply, including failures', async () => {
  const {agent, updates} = host();
  agent.client.stream = async (_cid, _content, emit) => {
    await emit({type: 'commentary', content: 'I will read the entry point before changing the game.'});
    await emit({type: 'message', message: {interim: true}});
    await emit({type: 'tool_start', name: 'read_file', arguments: {path: 'test.cpp'}});
    assert.equal(updates.at(-1).running, true);
    assert.equal(updates.at(-1).messages.at(-1).activity[0].label, 'Reading test.cpp');
    assert.equal(updates.at(-1).messages.at(-1).activity[0].status, 'running');
    await emit({type: 'tool_result', name: 'read_file', ok: true});
    await emit({type: 'tool_start', name: 'run_shell', arguments: {command: 'make test'}});
    await emit({type: 'tool_result', name: 'run_shell', ok: false, summary: 'Test failed'});
    await emit({type: 'delta', content: 'The edit is ready, but the test failed.'});
    await emit({type: 'message'});
  };
  await agent.send('Build the game');
  const reply = updates.at(-1);
  assert.equal(reply.running, false);
  assert.match(reply.messages[1].content, /read the entry point/);
  assert.equal(reply.messages[1].activity[0].status, 'done');
  assert.equal(reply.messages[1].activity[1].status, 'failed');
  assert.equal(reply.messages[2].content, 'The edit is ready, but the test failed.');
});

test('stopping a tool keeps its history without marking it successful', async () => {
  const {agent, updates} = host();
  agent.client.stream = async (_cid, _content, emit) => { await emit({type: 'tool_start', name: 'write_file', arguments: {path: 'game.cpp'}}); agent.controller.abort(); };
  await agent.send('Build');
  assert.equal(updates.at(-1).messages[1].activity[0].status, 'interrupted');
});

test('attachments send fresh bounded source while showing filenames in the chat', async () => {
  const temp = fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'olith-context-'));
  try {
    fs.writeFileSync(path.join(temp, 'source.cpp'), 'old');
    const {agent, updates} = host(); agent.client.session.root = temp;
    await agent.addFiles([{scheme: 'file', fsPath: path.join(temp, 'source.cpp')}]);
    await agent.addFiles([{scheme: 'file', fsPath: path.join(temp, 'source.cpp')}]);
    assert.equal(agent.attachments.length, 1, 'Duplicate selections produce one attachment');
    fs.writeFileSync(path.join(temp, 'source.cpp'), 'new source');
    agent.client.stream = async (_cid, content) => { assert.match(content, /new source/); assert.doesNotMatch(content, /old/); };
    await agent.send('Use this file');
    assert.equal(agent.attachments.length, 0);
    assert.equal(updates.at(-1).messages[0].content, 'Use this file\n\nFiles: source.cpp');
    fs.writeFileSync(path.join(temp, 'large.txt'), 'a'.repeat(50000));
    await agent.addFiles([{scheme: 'file', fsPath: path.join(temp, 'large.txt')}]);
    const context = await agent.fileContext(); assert(context.length < 25000); assert.match(context, /"truncated":true/);
  } finally { fs.rmSync(temp, {recursive: true}); }
});

test('outside, symlink and binary attachments are rejected and failed sends keep context', async () => {
  const temp = fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'olith-context-'));
  try {
    const {agent, updates} = host(); agent.client.session.root = temp;
    await assert.rejects(agent.addFiles([{scheme: 'file', fsPath: path.join(temp, '..', 'secret.txt')}]), /outside/);
    fs.symlinkSync('/etc/passwd', path.join(temp, 'linked.txt'));
    await assert.rejects(agent.addFiles([{scheme: 'file', fsPath: path.join(temp, 'linked.txt')}]), /symlink/);
    fs.writeFileSync(path.join(temp, 'binary.bin'), Buffer.from([0,1,2]));
    await agent.addFiles([{scheme: 'file', fsPath: path.join(temp, 'binary.bin')}]);
    await assert.rejects(agent.send('Read it'), /binary/);
    assert.equal(agent.attachments.length, 1); assert(!updates.some(m => m.type === 'submitted'));
  } finally { fs.rmSync(temp, {recursive: true}); }
});

test('the active file path follows editor changes without crossing the paired root', async () => {
  const {agent, editor} = host();
  editor.window.activeTextEditor = {document: {uri: {scheme: 'file', fsPath: '/project/new.cpp'}}};
  agent.client.stream = async (_cid, content) => assert.match(content, /Active editor file.*"new.cpp"/);
  await agent.send('Fix this file');
  editor.window.activeTextEditor.document.uri.fsPath = '/another-project/private.cpp';
  agent.client.stream = async (_cid, content) => assert.equal(content, 'Fix this file');
  await agent.send('Fix this file');
});

test('workspace selection restores only that folder and never reuses the old root session', async () => {
  const temp = fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'olith-folder-'));
  try {
    const folder = {name: 'Other', uri: {scheme: 'file', fsPath: temp}};
    const {agent} = host({window: {showQuickPick: async () => ({folder})}, workspace: {workspaceFolders: [folder]}});
    agent.attachments = [{id: 'old', path: 'old.txt'}];
    agent.restore = async root => { assert.equal(root, temp); assert.equal(agent.client, null); };
    agent.connect = async root => { assert.equal(root, temp); agent.client = {session: {root}}; };
    await agent.pickWorkspace();
    assert.equal(agent.client.session.root, temp); assert.equal(agent.attachments.length, 0);
    agent.running = true; await assert.rejects(agent.pickWorkspace(), /stop the task/);
  } finally { fs.rmSync(temp, {recursive: true}); }
});

test('streamed turns stay separate even when source contains transcript-like labels', async () => {
  const {agent, updates} = host();
  agent.client.stream = async (_cid, _content, emit) => {
    await emit({type: 'delta', content: 'Read this:\n\nYou: inside source'});
    await emit({type: 'message'});
    await emit({type: 'delta', content: 'Finished.'});
    await emit({type: 'message'});
    await emit({type: 'todos', todos: [{status: 'completed', content: 'Inspect source'}]});
  };
  await agent.send('Explain the code');
  const last = updates.at(-1);
  assert.equal(last.running, false);
  assert.equal(last.messages.length, 3);
  assert.equal(last.messages[1].content, 'Read this:\n\nYou: inside source');
  assert.equal(last.messages[2].content, 'Finished.');
  assert.equal(last.tasks[0].status, 'completed');
  assert.equal(updates[0].type, 'submitted');
  assert.equal(updates[0].content, 'Explain the code');
});

test('failed connection preserves the draft by withholding send acknowledgement', async () => {
  const {agent, updates} = host();
  agent.ensure = async () => { throw new Error('Not connected'); };
  await assert.rejects(agent.send('Keep this draft'), /Not connected/);
  assert.equal(updates.length, 0);
  assert.equal(agent.messages.length, 0);
});

test('saved pairing reconnects after Olith starts without asking for another code', async () => {
  let online = false, stores = 0;
  class FakeClient {
    constructor(url, session) { this.url = url; this.session = session; }
    route(suffix = '') { return suffix; }
    async json(route) {
      if (!online) throw new Error('fetch failed');
      if (route === '/edits') return {edits: []};
      if (route === '/chats/chat') return {messages: [{role: 'user', content: 'Saved question'}], model_id: 'coder:small'};
      throw new Error('Unexpected request');
    }
  }
  const {agent, updates} = host({workspace: {isTrusted: true}}, FakeClient);
  agent.client = null; agent.cid = null; agent.online = false;
  agent.folder = async () => '/project';
  agent.context = {secrets: {get: async () => JSON.stringify({url: 'http://localhost:7878', session: {id: 'saved', token: 'saved-secret', root: '/project'}, cid: 'chat'}), store: async () => stores++}};
  agent.connect = async () => assert.fail('A saved pairing must not ask for another code');
  await agent.restore();
  assert.equal(agent.client.session.id, 'saved'); assert.equal(agent.online, false);
  assert.equal(updates.at(-1).paired, true); assert.equal(updates.at(-1).connected, false);
  online = true; await agent.poll();
  assert.equal(agent.online, true); assert.equal(updates.at(-1).connected, true);
  assert.equal(agent.messages[0].content, 'Saved question');
  online = false; await agent.poll(); assert.equal(agent.client.session.id, 'saved');
  online = true; await agent.poll(); assert.equal(agent.online, true);
  assert.equal(stores, 0, 'Outages do not overwrite the saved credential');
});

test('a revoked pairing is not treated as a temporary network outage', async () => {
  const {agent, updates} = host();
  agent.client.route = route => route;
  agent.client.json = async () => { throw new Error('Olith 401: VS Code pairing removed'); };
  await agent.poll();
  assert.equal(agent.client, null); assert.equal(updates.at(-1).paired, false);
});

test('disconnect clears local credentials even while Olith is offline', async () => {
  const {agent} = host(); let forgotten;
  agent.stop = async () => { throw new Error('fetch failed'); };
  agent.client.route = () => '/session'; agent.client.json = async () => { throw new Error('fetch failed'); };
  agent.secretKey = 'saved-key'; agent.context = {secrets: {delete: async key => { forgotten = key; }}};
  await agent.disconnect(); assert.equal(forgotten, 'saved-key'); assert.equal(agent.client, null);
});

test('model selection persists before changing the displayed selection and is sent to inference', async () => {
  const {agent, updates} = host();
  agent.models.push({id: 'coder:large', label: 'Large coder'});
  agent.client.route = route => route;
  agent.client.json = async (route, body) => {
    assert.equal(route, '/chats/chat/model'); assert.equal(body.model_key, 'coder:large');
    assert.equal(agent.modelKey, 'coder:small'); return {model_key: 'coder:large'};
  };
  await agent.selectModel('coder:large');
  assert.equal(updates.at(-1).modelKey, 'coder:large');
  agent.client.stream = async (_cid, _content, _emit, _signal, modelKey) => assert.equal(modelKey, 'coder:large');
  await agent.send('Use my selected model');
  agent.running = true;
  await assert.rejects(agent.selectModel('coder:small'), /Stop the current task/);
});

test('failed model changes retain the previous selection', async () => {
  const {agent} = host();
  agent.models.push({id: 'coder:large', label: 'Large coder'});
  agent.client.route = route => route;
  agent.client.json = async () => { throw new Error('Model was removed'); };
  await assert.rejects(agent.selectModel('coder:large'), /Model was removed/);
  assert.equal(agent.modelKey, 'coder:small'); assert.equal(agent.modelLoading, false);
});

test('model refresh keeps a valid chat choice and reconciles removed models', async () => {
  const {agent} = host(); delete agent.refreshModels;
  agent.client.route = route => route;
  let models = [{id: 'coder:small', label: 'Small coder'}, {id: 'coder:large', label: 'Large coder'}];
  agent.client.json = async route => { assert.equal(route, '/models'); return {models, active: 'coder:large'}; };
  await agent.refreshModels(); assert.equal(agent.modelKey, 'coder:small');
  models = [models[1]];
  await agent.refreshModels(); assert.equal(agent.modelKey, 'coder:large');
  models = [];
  await agent.refreshModels(); assert.equal(agent.modelKey, null); assert.equal(agent.modelLoading, false);
});
