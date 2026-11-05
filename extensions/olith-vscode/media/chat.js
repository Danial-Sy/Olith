// SPDX-License-Identifier: AGPL-3.0-or-later
(() => {
  const vscode = acquireVsCodeApi();
  const $ = id => document.getElementById(id);
  const prompt = $('prompt'), conversation = $('conversation'), messages = $('messages');
  let state = {messages: [], tasks: [], running: false}, pending = null, frame = null;
  let submitted = null, followNext = false, chatId = null;
  prompt.value = vscode.getState()?.draft || '';
  const resize = () => { prompt.style.height = 'auto'; prompt.style.height = Math.min(180, prompt.scrollHeight) + 'px'; };
  const saveDraft = () => { vscode.setState({draft: prompt.value}); resize(); };
  prompt.addEventListener('input', saveDraft);
  $('attachments').addEventListener('click', event => {
    const button = event.target.closest('[data-remove]');
    if (button) vscode.postMessage({action: 'remove-file', id: button.dataset.remove});
  });
  document.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', () => vscode.postMessage({action: button.dataset.action})));
  document.querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => { prompt.value = button.dataset.prompt; saveDraft(); prompt.focus(); }));
  $('model').addEventListener('change', () => {
    $('send').disabled = true; $('model').disabled = true;
    vscode.postMessage({action: 'model', model: $('model').value});
  });
  $('form').addEventListener('submit', event => {
    event.preventDefault();
    if (state.running) { vscode.postMessage({action: 'stop'}); return; }
    if (!prompt.value.trim() || submitted !== null) return;
    submitted = prompt.value; vscode.postMessage({action: 'send', content: submitted});
  });
  $('send').addEventListener('click', event => {
    if (state.running) { event.preventDefault(); vscode.postMessage({action: 'stop'}); }
  });
  prompt.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault(); if (!state.running) $('form').requestSubmit();
    }
  });
  messages.addEventListener('click', event => {
    const button = event.target.closest('.copy-code');
    if (button) { button.textContent = 'Copying…'; vscode.postMessage({action: 'copy', content: button.closest('.code-block').querySelector('code').textContent}); }
  });
  function render(next) {
    const nearBottom = followNext || chatId !== next.cid || conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 70;
    followNext = false;
    if (chatId !== next.cid) { messages.replaceChildren(); chatId = next.cid; }
    state = next;
    document.body.classList.toggle('running', !!state.running);
    $('connection').textContent = state.connected ? 'Connected' : state.paired ? 'Reconnecting' : 'Not connected';
    $('connection').classList.toggle('connected', !!state.connected);
    $('project-name').textContent = state.project || 'Your local workspace';
    $('project').title = state.root || 'Open a local folder in VS Code to get started';
    const projectBadge = document.querySelector('.project-badge');
    projectBadge.textContent = state.connected ? 'CONNECTED' : state.paired ? 'PAIRED · OFFLINE' : 'NOT CONNECTED';
    projectBadge.classList.toggle('connected', !!state.connected);
    $('status').textContent = state.status;
    $('status').title = state.status;
    $('connect-cta').hidden = !!state.connected || !!state.paired;
    document.querySelector('[data-action="workspace"]').disabled = !!state.running || !!state.modelLoading;
    $('add-files').disabled = !!state.running || !!state.modelLoading;
    $('active-file').disabled = !!state.running || !state.activeFile || !state.connected;
    $('active-file').textContent = state.activeFile ? 'Use ' + state.activeFile.split('/').at(-1) : 'Use active file';
    $('active-file').title = state.activeFile ? 'Attach ' + state.activeFile : 'Open a file inside the paired workspace';
    const files = state.attachments || [], attachmentKey = JSON.stringify([files, state.running]);
    $('attachments').hidden = !files.length;
    if ($('attachments').dataset.key !== attachmentKey) {
      $('attachments').replaceChildren(...files.map(file => {
        const chip = document.createElement('span'), label = document.createElement('span'), remove = document.createElement('button');
        chip.className = 'file-chip'; chip.title = file.path; label.textContent = file.path.split('/').at(-1);
        remove.type = 'button'; remove.dataset.remove = file.id; remove.textContent = '×'; remove.disabled = !!state.running;
        remove.setAttribute('aria-label', 'Remove ' + file.path); chip.append(label, remove); return chip;
      }));
      $('attachments').dataset.key = attachmentKey;
    }
    const models = state.models || [], modelSelect = $('model');
    const modelListKey = JSON.stringify([state.connected, models, !models.length && state.modelLoading]);
    if (modelSelect.dataset.key !== modelListKey) {
      const choices = models.length && state.connected ? models : [{id: '', label: state.connected ? (state.modelLoading ? 'Loading models…' : 'No Agentic models available') : 'Connect to choose a model'}];
      modelSelect.replaceChildren(...choices.map(model => {
        const option = document.createElement('option'); option.value = model.id; option.textContent = model.label || model.id; return option;
      }));
      modelSelect.dataset.key = modelListKey;
    }
    modelSelect.value = state.connected ? state.modelKey || '' : '';
    modelSelect.disabled = !state.connected || !models.length || state.running || state.modelLoading;
    modelSelect.title = (state.connected && state.modelKey) || 'Models assigned to Agentic in Olith';
    $('refresh-models').disabled = !state.connected || state.running || state.modelLoading;
    $('welcome').hidden = state.messages.length > 0;
    document.querySelector('[data-action="new"]').disabled = !!state.running || !!state.modelLoading;
    const send = $('send');
    send.disabled = !!state.modelLoading && !state.running;
    send.title = state.running ? 'Stop task' : 'Send message'; send.setAttribute('aria-label', send.title);
    for (let index = 0; index < state.messages.length; index++) {
      const message = state.messages[index];
      let node = messages.children[index];
      if (!node) {
        node = document.createElement('article');
        const heading = document.createElement('div'); heading.className = 'message-heading';
        if (message.role === 'assistant') heading.append($('assistant-mark').content.cloneNode(true));
        const label = document.createElement('span'); label.textContent = message.role === 'user' ? 'You' : 'Olith'; heading.append(label);
        const body = document.createElement('div'); body.className = 'message-body';
        const activity = document.createElement('ol'); activity.className = 'tool-activity'; activity.setAttribute('aria-label', 'Live activity');
        node.append(heading, body, activity); messages.append(node);
      }
      const active = state.running && index === state.messages.length - 1 && message.role === 'assistant';
      const activityKey = JSON.stringify(message.activity || []);
      node.hidden = message.role === 'assistant' && !message.content && !active && !message.activity?.length;
      node.className = 'message ' + message.role + (active ? (message.content ? ' streaming' : ' working') : '');
      if (node._content !== message.content || node._active !== active || node._activity !== activityKey || (!message.content && active && node._status !== state.status)) {
        const body = node.querySelector('.message-body');
        const scrolls = [...body.querySelectorAll('pre')].map(pre => pre.scrollLeft);
        if (message.role === 'user') body.textContent = message.content;
        else if (message.content) body.innerHTML = OlithMarkdown.renderMarkdown(message.content);
        else { body.replaceChildren(); if (active && !message.activity?.length) { const thinking = document.createElement('span'); thinking.className = 'thinking'; thinking.textContent = state.status || 'Planning the next step…'; body.append(thinking); } }
        body.querySelectorAll('pre').forEach((pre, i) => { pre.scrollLeft = scrolls[i] || 0; });
        node._content = message.content; node._active = active; node._status = state.status;
      }
      if (node._activity !== activityKey) {
        node.querySelector('.tool-activity').replaceChildren(...(message.activity || []).map(step => {
          const item = document.createElement('li'), icon = document.createElement('span'), label = document.createElement('span');
          item.className = 'step-' + step.status; icon.className = 'step-icon';
          icon.textContent = step.status === 'running' ? '◉' : step.status === 'done' ? '✓' : step.status === 'failed' ? '×' : '○';
          icon.setAttribute('aria-label', step.status); label.textContent = step.label; item.title = step.detail || step.label;
          item.append(icon, label); return item;
        }));
        node._activity = activityKey;
      }
    }
    while (messages.children.length > state.messages.length) messages.lastElementChild.remove();
    $('tasks').hidden = !state.tasks.length;
    const complete = state.tasks.filter(task => ['completed', 'done', 'cancelled'].includes(task.status)).length;
    const currentTask = state.tasks.find(task => ['in_progress', 'running'].includes(task.status));
    $('task-summary').textContent = currentTask ? (currentTask.activeForm || currentTask.content || currentTask.title) : `Task progress · ${complete}/${state.tasks.length}`;
    const taskKey = JSON.stringify(state.tasks);
    if ($('task-list').dataset.key !== taskKey) {
      $('task-list').replaceChildren(...state.tasks.map(task => {
        const item = document.createElement('li'), icon = document.createElement('span'), label = document.createElement('span');
        const status = task.status || 'pending';
        const done = ['completed', 'done'].includes(status), active = ['in_progress', 'running'].includes(status);
        item.className = active ? 'task-active' : ''; icon.className = 'task-icon'; icon.textContent = done ? '✓' : status === 'cancelled' ? '×' : active ? '◉' : '○';
        icon.setAttribute('aria-label', status.replaceAll('_', ' ')); label.textContent = (active && task.activeForm) || task.content || task.text || task.title || 'Task';
        item.append(icon, label); return item;
      }));
      $('task-list').dataset.key = taskKey;
    }
    if (nearBottom) conversation.scrollTop = conversation.scrollHeight;
  }
  window.addEventListener('message', event => {
    const message = event.data;
    if (message.type === 'submitted') {
      if (prompt.value === message.content) { prompt.value = ''; saveDraft(); }
      submitted = null; followNext = true; return;
    }
    if (message.type === 'send-finished') { submitted = null; return; }
    if (message.type === 'copied') {
      messages.querySelectorAll('.copy-code').forEach(button => { if (button.textContent === 'Copying…') { button.textContent = 'Copied'; setTimeout(() => { button.textContent = 'Copy'; }, 1600); } }); return;
    }
    if (message.type !== 'state') return;
    pending = message;
    if (!frame) frame = requestAnimationFrame(() => { frame = null; render(pending); });
  });
  document.addEventListener('visibilitychange', () => document.body.classList.toggle('background', document.hidden));
  resize(); vscode.postMessage({action: 'ready'});
})();
