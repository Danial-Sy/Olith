// SPDX-License-Identifier: AGPL-3.0-or-later
// Run with code --extensionDevelopmentPath=... --extensionTestsPath=.../test/host.js <temporary folder>
const assert = require('node:assert/strict');
const vscode = require('vscode');
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const {Client, applyProposal} = require('../client');

async function run() {
  const root = vscode.workspace.workspaceFolders[0].uri.fsPath;
  assert(path.basename(root).startsWith('olith-vscode-host'), 'Use an isolated olith-vscode-host temporary folder');
  for (const name of ['host-results.json', 'created.txt']) {
    try { await fs.unlink(path.join(root, name)); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  const extension = vscode.extensions.getExtension('olith.olith-vscode');
  assert(extension, 'Extension discovered'); const activated = await extension.activate();
  const commands = await vscode.commands.getCommands();
  assert(commands.includes('workbench.view.extension.olith-agent'), 'Fresh sidebar registration replaces the cached legacy container');
  assert(!commands.includes('workbench.view.extension.olith'), 'Legacy H container is no longer registered');
  for (const name of ['connect','sendSelection','fixDiagnostic','review','stop','disconnect','addFiles','chooseWorkspace']) assert(commands.includes('olith.' + name), name + ' registered');
  await vscode.commands.executeCommand('olith.chat.focus');
  for (let tries = 0; tries < 80 && !activated.instance.viewReady; tries++) await new Promise(resolve => setTimeout(resolve, 100));
  const view = activated.instance.view;
  assert(view, 'Sidebar webview resolved');
  assert(activated.instance.viewReady, 'Production webview scripts executed and completed the host handshake');
  assert(view.webview.html.includes('chat.css') && view.webview.html.includes('markdown.js'), 'Styled sidebar assets loaded');
  assert.equal(view.webview.options.localResourceRoots.length, 1, 'Only packaged media assets are exposed');
  const file = path.join(root, 'app.txt'); await fs.writeFile(file, 'before\n');
  const document = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
  await vscode.window.showTextDocument(document);
  const edit = {id: 'host', path: 'app.txt', before_sha256: crypto.createHash('sha256').update('before\n').digest('hex'), content: 'after\n', expires_at: Date.now() / 1000 + 60};
  await applyProposal(vscode, root, edit);
  assert.equal(document.getText(), 'after\n'); assert.equal(await fs.readFile(file, 'utf8'), 'after\n');
  await vscode.commands.executeCommand('undo');
  assert.equal(document.getText(), 'before\n', 'Olith edit participates in editor undo');
  await assert.rejects(applyProposal(vscode, root, {...edit, before_sha256: crypto.createHash('sha256').update('after\n').digest('hex')}), /unsaved/);
  await document.save();
  const collection = vscode.languages.createDiagnosticCollection('olith-host-test');
  const diagnostic = new vscode.Diagnostic(new vscode.Range(0, 0, 0, 6), 'Example problem', vscode.DiagnosticSeverity.Error);
  collection.set(document.uri, [diagnostic]);
  const actions = await vscode.commands.executeCommand('vscode.executeCodeActionProvider', document.uri, diagnostic.range, vscode.CodeActionKind.QuickFix.value);
  assert(actions.some(a => a.command?.command === 'olith.fixDiagnostic'), 'Fix with Olith appears on diagnostics');
  collection.dispose();
  await applyProposal(vscode, root, {...edit, path: 'created.txt', before_sha256: null, content: 'new\n'});
  assert.equal(await fs.readFile(path.join(root, 'created.txt'), 'utf8'), 'new\n');
  const checks = ['activation', 'commands', 'fresh-sidebar-icon-registration', 'styled-webview', 'WorkspaceEdit', 'save', 'undo', 'dirty-buffer-guard', 'diagnostic-code-action', 'create-file'];
  if (process.env.OLITH_IDE_SMOKE_URL) {
    const client = new Client(process.env.OLITH_IDE_SMOKE_URL);
    const {code} = await client.json('/api/ide/pair-code', {});
    const session = await client.json('/api/ide/connect', {code, root});
    const paired = new Client(client.url, session);
    activated.instance.client = paired;
    activated.instance.online = true;
    activated.instance.secretKey = 'olith.session.' + root;
    await activated.instance.newChat();
    assert.equal(activated.instance.modelKey, 'fixture', 'Uses the default Olith Agentic model');
    await activated.instance.selectModel('fixture-large');
    assert.equal(activated.instance.modelKey, 'fixture-large');
    const restored = await paired.json(paired.route(`/chats/${activated.instance.cid}`));
    assert.equal(restored.model_id, 'fixture-large', 'Model selection is saved with the chat');
    activated.instance.client = null; activated.instance.cid = null; activated.instance.modelKey = null;
    await activated.instance.restore();
    assert.equal(activated.instance.modelKey, 'fixture-large', 'Restoring the editor session restores its saved model');
    const source = path.join(root, 'stream.txt'); await fs.writeFile(source, 'before\n');
    const sourceDocument = await vscode.workspace.openTextDocument(vscode.Uri.file(source));
    await vscode.window.showTextDocument(sourceDocument);
    await activated.instance.addFiles([sourceDocument.uri]);
    assert.equal(activated.instance.attachments[0].path, 'stream.txt');
    const progress = [], event = activated.instance.event.bind(activated.instance);
    activated.instance.event = async update => {
      await event(update);
      if (update.type === 'tool_start') progress.push({type: update.type, name: update.name, running: activated.instance.running, source: await fs.readFile(source, 'utf8'), label: activated.instance.messages.at(-1).activity.at(-1).label});
      if (update.type === 'commentary') progress.push({type: update.type, content: update.content});
    };
    await activated.instance.send('Change before to after');
    assert.equal(sourceDocument.getText(), 'after\n', 'Stream tool edits applied to the open buffer');
    assert.equal(await fs.readFile(source, 'utf8'), 'after\n');
    assert(activated.instance.transcript.includes('Changed the source through VS Code.'));
    assert(activated.instance.messages.some(m => m.role === 'assistant' && m.content.includes('Changed the source through VS Code.')), 'Structured reply reaches the new renderer');
    assert(progress.some(p => p.type === 'commentary' && p.content.includes('inspect stream.txt')));
    assert(progress.some(p => p.name === 'edit_file' && p.running && p.source === 'before\n' && p.label === 'Editing stream.txt'), 'Live edit progress appears before disk changes');
    assert(activated.instance.messages.some(m => m.activity?.some(a => a.name === 'edit_file' && a.status === 'done')));
    assert.equal(activated.instance.attachments.length, 0);
    const review = await paired.json(paired.route(`/chats/${activated.instance.cid}/changes`));
    assert(review.files.some(f => f.file === 'stream.txt'), 'Task review includes editor changes');
    const savedChat = activated.instance.cid;
    clearInterval(activated.instance.timer);
    while (activated.instance.polling) await new Promise(resolve => setTimeout(resolve, 20));
    await paired.json('/__fixture/restart-bridge', {});
    activated.instance.online = false;
    await activated.instance.poll();
    assert.equal(activated.instance.client.session.id, paired.session.id, 'The same pairing reconnects after the bridge restarts');
    assert.equal(activated.instance.client.session.token, paired.session.token, 'Reconnection does not replace the saved credential');
    assert.equal(activated.instance.online, true);
    assert.equal(activated.instance.cid, savedChat, 'The saved chat stays linked');
    await activated.instance.disconnect();
    checks.push('pairing', 'installed-model-picker', 'saved-model-selection', 'restored-model-selection', 'selected-model-generation', 'authenticated-stream', 'real-agent-tool-loop', 'editor-save-ack', 'task-review', 'file-context', 'live-commentary', 'progress-before-edit', 'completed-tool-activity', 'persistent-pairing-restart');
  }
  await fs.writeFile(path.join(root, 'host-results.json'), JSON.stringify({passed: true, checks}));
}
module.exports = {run};
