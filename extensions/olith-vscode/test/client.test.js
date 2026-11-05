// SPDX-License-Identifier: AGPL-3.0-or-later
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const {Client, serverUrl, parseSSE, safePath, applyProposal} = require('../client');

test('backend URL accepts only loopback HTTP with no credentials or redirects', () => {
  assert.equal(serverUrl('http://127.0.0.1:7878'), 'http://127.0.0.1:7878');
  for (const url of ['https://localhost', 'http://evil.invalid', 'http://localhost.evil.invalid', 'http://u:p@localhost', 'http://localhost/path', 'http://localhost/?token=x']) assert.throws(() => serverUrl(url));
});
test('authenticated requests use a header and surface actionable errors', async () => {
  let received;
  const client = new Client('http://localhost:7878', {id: 'session', token: 'secret'}, async (url, options) => {
    received = {url, options}; return new Response(JSON.stringify({detail: 'Buffer changed'}), {status: 409});
  });
  await assert.rejects(client.json(client.route('/edits')), /409: Buffer changed/);
  assert.equal(received.options.headers.Authorization, 'Bearer secret');
  assert.equal(received.options.redirect, 'error');
  assert(!received.url.includes('secret'));
});
test('SSE parser preserves split Unicode, CRLF and event order', async () => {
  const bytes = Buffer.from('data: {"type":"delta","content":"🙂"}\r\n\r\ndata: {"type":"done"}\n\n');
  async function* chunks() { for (const byte of bytes) yield Uint8Array.of(byte); }
  const events = []; await parseSSE(chunks(), async e => events.push(e));
  assert.deepEqual(events, [{type: 'delta', content: '🙂'}, {type: 'done'}]);
});
test('SSE parser rejects an incomplete response instead of silently dropping it', async () => {
  async function* chunks() { yield Buffer.from('data: {"type":"done"}'); }
  await assert.rejects(parseSSE(chunks(), () => {}), /event completed/);
});
test('streaming reports an early disconnect and accepts a completed task', async () => {
  const session = {id: 'session', token: 'secret'};
  const client = new Client('http://localhost:7878', session, async () => new Response('data: {"type":"delta","content":"partial"}\n\n'));
  await assert.rejects(client.stream('chat', 'fix', () => {}), /before the task finished/);
  client.fetch = async () => new Response('data: {"type":"done"}\n\n');
  await client.stream('chat', 'fix', () => {});
});
test('stream sends the chosen model for both agent model fields', async () => {
  const client = new Client('http://localhost:7878', {id: 'session', token: 'secret'}, async (_url, options) => {
    assert.deepEqual(JSON.parse(options.body), {content: 'fix', mode: 'agentic', model_key: 'coder:local', agent_model: 'coder:local'});
    return new Response('data: {"type":"done"}\n\n');
  });
  await client.stream('chat', 'fix', () => {}, undefined, 'coder:local');
});

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'olith-extension-'));
  t.after(() => fs.rm(root, {recursive: true, force: true}));
  await fs.writeFile(path.join(root, 'app.txt'), 'before\n'); return root;
}
function fakeVSCode(root, dirty = false) {
  const document = {uri: {toString: () => path.join(root, 'app.txt')}, isDirty: dirty, getText: () => 'before\n', positionAt: n => n, save: async () => true};
  const operations = [];
  return {
    operations, document,
    Uri: {file: value => ({fsPath: value, toString: () => value})},
    Range: class { constructor(...args) { this.args = args; } },
    EndOfLine: {LF: 1, CRLF: 2},
    TextEdit: {replace: (range, content) => ({range, content}), setEndOfLine: eol => ({eol})},
    WorkspaceEdit: class { createFile(uri) { operations.push(['create', uri.fsPath]); } set(uri, edits) { operations.push(['edit', uri.fsPath, edits]); } },
    workspace: {textDocuments: [document], openTextDocument: async () => document, applyEdit: async () => true}
  };
}
const proposal = overrides => ({id: 'edit', path: 'app.txt', before_sha256: crypto.createHash('sha256').update('before\n').digest('hex'), content: 'after\n', expires_at: Date.now() / 1000 + 60, ...overrides});

test('workspace edits cannot escape, target git internals, or follow symlinks', async t => {
  const root = await fixture(t);
  for (const rel of ['../outside', '/tmp/outside', '.git/config', 'a/../../outside', 'C:\\outside', 'a\\..\\outside']) await assert.rejects(safePath(root, rel));
  await fs.symlink(os.tmpdir(), path.join(root, 'link'));
  await assert.rejects(safePath(root, 'link/outside'), /symlink/);
});
test('unchanged source applies via WorkspaceEdit with explicit line endings', async t => {
  const root = await fixture(t); const vscode = fakeVSCode(root);
  await applyProposal(vscode, root, proposal());
  assert.equal(vscode.operations[0][0], 'edit');
  assert.equal(vscode.operations[0][2][0].content, 'after\n');
  assert.equal(vscode.operations[0][2][1].eol, 1);
});
test('new files use createFile and never overwrite an unexpected file', async t => {
  const root = await fixture(t); const vscode = fakeVSCode(root);
  await applyProposal(vscode, root, proposal({path: 'new.txt', before_sha256: null}));
  assert.equal(vscode.operations[0][0], 'create');
  await assert.rejects(applyProposal(vscode, root, proposal({before_sha256: null})), /changed on disk/);
});
test('unsaved buffers, changed disk content and expired edits are refused', async t => {
  const root = await fixture(t);
  await assert.rejects(applyProposal(fakeVSCode(root, true), root, proposal()), /unsaved/);
  await fs.writeFile(path.join(root, 'app.txt'), 'user edited\n');
  await assert.rejects(applyProposal(fakeVSCode(root), root, proposal()), /changed on disk/);
  await assert.rejects(applyProposal(fakeVSCode(root), root, proposal({expires_at: 0})), /expired/);
});
test('editor buffers with unsupported encoding are refused', async t => {
  const root = await fixture(t); const vscode = fakeVSCode(root);
  vscode.document.getText = () => 'different';
  await assert.rejects(applyProposal(vscode, root, proposal()), /differs from disk/);
});
test('failed editor apply or save is reported', async t => {
  const root = await fixture(t); const vscode = fakeVSCode(root);
  vscode.workspace.applyEdit = async () => false;
  await assert.rejects(applyProposal(vscode, root, proposal()), /rejected/);
  vscode.workspace.applyEdit = async () => true; vscode.document.save = async () => false;
  await assert.rejects(applyProposal(vscode, root, proposal()), /could not save/);
});
