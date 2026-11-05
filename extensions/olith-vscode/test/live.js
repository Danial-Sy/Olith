// SPDX-License-Identifier: AGPL-3.0-or-later
// Live run: real backend, real model, real editor saves with format-on-save on.
// Started by scripts/ide_live_test.py, which writes live-config.json beside the workspace.
const vscode = require('vscode');
const fs = require('node:fs/promises');
const path = require('node:path');
const {Client} = require('../client');

async function run() {
  const root = vscode.workspace.workspaceFolders[0].uri.fsPath;
  const results = {finished: false, events: [], error: null};
  try {
    const config = JSON.parse(await fs.readFile(path.join(root, '..', 'live-config.json'), 'utf8'));
    const extension = vscode.extensions.getExtension('olith.olith-vscode');
    const instance = (await extension.activate()).instance;
    const client = new Client(config.url);
    const {code} = await client.json('/api/ide/pair-code', {});
    const session = await client.json('/api/ide/connect', {code, root: await fs.realpath(root)});
    instance.client = new Client(client.url, session);
    instance.online = true;
    instance.secretKey = 'olith.session.' + root;
    await instance.newChat();
    await instance.selectModel(config.model);
    results.model = instance.modelKey;
    const event = instance.event.bind(instance);
    instance.event = async update => {
      if (['tool_start', 'tool_result', 'verification', 'error'].includes(update.type)) {
        results.events.push({type: update.type, name: update.name, ok: update.ok, summary: String(update.summary || '').slice(0, 300)});
      }
      await event(update);
    };
    const started = Date.now();
    await instance.send(config.task);
    results.elapsed_s = Math.round((Date.now() - started) / 1000);
    results.finished = true;
    results.reply = instance.messages.filter(m => m.role === 'assistant' && m.content).map(m => m.content).at(-1) || '';
    results.activity = instance.messages.flatMap(m => m.activity || []).map(a => ({name: a.name, status: a.status, detail: a.detail}));
    clearInterval(instance.timer);
  } catch (error) {
    results.error = String(error && error.stack || error);
  }
  await fs.writeFile(path.join(root, '..', 'live-results.json'), JSON.stringify(results, null, 2));
}
module.exports = {run};
