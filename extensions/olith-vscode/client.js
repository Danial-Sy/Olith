// SPDX-License-Identifier: AGPL-3.0-or-later
const path = require('node:path');
const fs = require('node:fs/promises');
const crypto = require('node:crypto');
const {version} = require('./package.json');

function serverUrl(value) {
  const url = new URL(value);
  if (url.protocol !== 'http:' || !['localhost', '127.0.0.1'].includes(url.hostname) || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('Olith must use a local HTTP loopback URL, for example http://127.0.0.1:7878');
  }
  return url.origin;
}

class Client {
  constructor(url, session, fetcher = fetch) { this.url = serverUrl(url); this.session = session; this.fetch = fetcher; }
  async request(route, body, method = body === undefined ? 'GET' : 'POST', signal) {
    const headers = {'Content-Type': 'application/json', 'X-Olith-Extension-Version': version};
    if (this.session) headers.Authorization = `Bearer ${this.session.token}`;
    const response = await this.fetch(this.url + route, {method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal: signal || AbortSignal.timeout(15000), redirect: 'error'});
    if (!response.ok) {
      let message;
      try { const data = await response.json(); message = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail); }
      catch { message = response.statusText; }
      throw new Error(`Olith ${response.status}: ${message}`);
    }
    return response;
  }
  route(suffix = '') { if (!this.session) throw new Error('Run Olith: Connect to Olith first'); return `/api/ide/${this.session.id}${suffix}`; }
  async json(route, body, method) { return (await this.request(route, body, method)).json(); }
  async stream(cid, content, onEvent, signal, modelKey) {
    const response = await this.request(this.route(`/chats/${cid}/send`), {content, mode: 'agentic', model_key: modelKey, agent_model: modelKey}, 'POST', signal);
    let finished = false;
    await parseSSE(response.body, async event => {
      if (['done', 'error', 'stopped', 'cancelled'].includes(event.type)) finished = true;
      await onEvent(event);
    });
    if (!finished) throw new Error('Olith disconnected before the task finished; review the chat before retrying');
  }
}

async function parseSSE(body, onEvent) {
  const decoder = new TextDecoder(); let buffer = '';
  const consume = async final => {
    let match;
    while ((match = /\r?\n\r?\n/.exec(buffer))) {
      const packet = buffer.slice(0, match.index); buffer = buffer.slice(match.index + match[0].length);
      const data = packet.split(/\r?\n/).filter(l => l.startsWith('data:')).map(l => l.slice(5).trimStart()).join('\n');
      if (data && data !== '[DONE]') await onEvent(JSON.parse(data));
    }
    if (final && buffer.trim()) throw new Error('Olith stream ended before an event completed');
  };
  for await (const chunk of body) { buffer += decoder.decode(chunk, {stream: true}); await consume(false); }
  buffer += decoder.decode(); await consume(true);
}

async function safePath(root, relative) {
  if (!relative || path.isAbsolute(relative) || relative.includes('\\') || relative.split('/').some(p => p === '..' || p === '.git' || p === '')) throw new Error('Edit path is outside the workspace');
  root = await fs.realpath(root);
  const target = path.resolve(root, relative);
  const rel = path.relative(root, target);
  if (!rel || rel.startsWith('..' + path.sep) || path.isAbsolute(rel)) throw new Error('Edit path is outside the workspace');
  let current = root;
  for (const part of rel.split(path.sep)) {
    current = path.join(current, part);
    try { if ((await fs.lstat(current)).isSymbolicLink()) throw new Error('Refusing a symlink edit'); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  return target;
}

async function applyProposal(vscode, root, proposal) {
  if (proposal.expires_at * 1000 <= Date.now()) throw new Error('Edit proposal expired');
  const target = await safePath(root, proposal.path);
  let before = null;
  try { before = await fs.readFile(target); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  const hash = before === null ? null : crypto.createHash('sha256').update(before).digest('hex');
  if (hash !== proposal.before_sha256) throw new Error('File changed on disk while Olith was working; retry with current source');
  const uri = vscode.Uri.file(target);
  let document = vscode.workspace.textDocuments.find(d => d.uri.toString() === uri.toString());
  if (document?.isDirty) throw new Error('File has unsaved editor changes; save or undo them before retrying');
  if (before !== null) {
    document = document || await vscode.workspace.openTextDocument(uri);
    if (document.getText() !== before.toString('utf8')) throw new Error('Editor buffer differs from disk (or uses an unsupported encoding)');
  }
  // No await between the last buffer/version check and applyEdit submission.
  if (proposal.expires_at * 1000 <= Date.now()) throw new Error('Edit proposal expired');
  const edit = new vscode.WorkspaceEdit();
  if (before === null) edit.createFile(uri, {overwrite: false, ignoreIfExists: false});
  const range = document ? new vscode.Range(document.positionAt(0), document.positionAt(document.getText().length)) : new vscode.Range(0, 0, 0, 0);
  edit.set(uri, [vscode.TextEdit.replace(range, proposal.content), vscode.TextEdit.setEndOfLine(proposal.content.includes('\r\n') ? vscode.EndOfLine.CRLF : vscode.EndOfLine.LF)]);
  if (!await vscode.workspace.applyEdit(edit)) throw new Error('VS Code rejected the workspace edit');
  document = await vscode.workspace.openTextDocument(uri);
  // The server checks saved bytes, including changes from format-on-save.
  if (!await document.save()) throw new Error('VS Code could not save the edited file');
  return target;
}

module.exports = {Client, serverUrl, parseSSE, safePath, applyProposal};
