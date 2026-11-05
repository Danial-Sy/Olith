// SPDX-License-Identifier: AGPL-3.0-or-later
const escape = value => String(value).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const {version} = require('./package.json');

function webviewHtml({nonce, source, asset}) {
  const resource = name => asset(name) + '?v=' + version;
  const icon = (name, paths) => `<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" class="icon icon-${name}">${paths}</svg>`;
  const plus = icon('plus', '<path d="M12 5v14M5 12h14"/>');
  const review = icon('review', '<path d="M8 3H5v18h14V3h-3M8 3h8v4H8zM9 12h6M9 16h4"/>');
  const connect = icon('connect', '<path d="m8 3 3 3-5 5-3-3m13 13-3-3 5-5 3 3M8 8l8 8m-9 1-4 4m14-14 4-4"/>');
  const arrow = icon('send', '<path d="M12 19V5m-6 6 6-6 6 6"/>');
  const folder = icon('folder', '<path d="M3 7V5h7l2 2h9v12H3z"/>');
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${escape(source)}; style-src ${escape(source)}; script-src 'nonce-${escape(nonce)}';">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <link rel="stylesheet" href="${escape(resource('chat.css'))}"><title>Olith</title></head><body>
    <header class="header"><div class="brand"><img src="${escape(asset('olith.svg'))}" alt="" width="24" height="28"><span>Olith</span><span class="connection" id="connection">Not connected</span></div>
      <nav aria-label="Chat controls"><button class="icon-button" data-action="connect" title="Connect to Olith" aria-label="Connect to Olith">${connect}</button><button class="icon-button" data-action="new" title="New chat" aria-label="New chat">${plus}</button><button class="icon-button" data-action="review" title="Review task changes" aria-label="Review task changes">${review}</button></nav></header>
    <div class="project" id="project"><button class="workspace-button" data-action="workspace" aria-label="Choose workspace folder" title="Choose workspace folder">${folder}<span id="project-name">Choose workspace folder</span></button><span class="project-badge">PROJECT</span></div>
    <main id="conversation" aria-label="Conversation" tabindex="0">
      <section class="welcome" id="welcome"><div class="welcome-mark"><img src="${escape(asset('olith.svg'))}" alt="Olith" width="56" height="62"></div>
        <h1>Your Local LLM Suite</h1>
        <button class="connect-button" data-action="connect" id="connect-cta">Connect to Olith <span aria-hidden="true">↗</span></button>
        <div class="starters"><span class="eyebrow">A PLACE TO START</span><button data-prompt="Explain how this project starts up">Explain this project <span aria-hidden="true">↗</span></button><button data-prompt="Find a useful improvement in this project and explain your plan before changing anything">Find the next improvement <span aria-hidden="true">↗</span></button></div>
      </section><div id="messages"></div>
    </main>
    <div class="bottom"><details class="tasks" id="tasks" hidden><summary><span class="task-dot" aria-hidden="true"></span><span id="task-summary">Task progress</span><span class="chevron" aria-hidden="true">⌄</span></summary><ol id="task-list"></ol></details>
      <div class="activity" id="activity"><span class="activity-dot" aria-hidden="true"></span><span id="status" role="status" aria-live="polite">Connect to your local Olith app.</span></div>
      <form id="form" class="composer"><div class="context-toolbar"><button type="button" data-action="files" id="add-files" title="Choose files from this workspace">${plus}<span>Add files</span></button><button type="button" data-action="active-file" id="active-file" title="Attach the active editor file" disabled>Use active file</button></div><div class="attachments" id="attachments" aria-label="Attached files" hidden></div><label class="sr-only" for="prompt">Message Olith</label><textarea id="prompt" rows="2" placeholder="What are we working on?" required></textarea>
        <div class="composer-toolbar"><div class="model-picker"><label for="model" class="model-label">Model</label><select id="model" aria-label="Olith model" title="Models assigned to Agentic in Olith" disabled><option>Connect to choose a model</option></select><button type="button" id="refresh-models" class="refresh-models" data-action="models" title="Refresh installed models" aria-label="Refresh installed models" disabled>↻</button></div><button id="send" class="send-button" type="submit" title="Send message" aria-label="Send message">${arrow}<span class="stop-square" aria-hidden="true"></span></button></div></form>
      <footer><span>Olith ${escape(version)}</span><span id="shortcut">Enter to send</span></footer></div>
    <template id="assistant-mark"><span class="response-mark"><img src="${escape(asset('olith.svg'))}" width="28" height="32" alt=""></span></template>
    <script nonce="${escape(nonce)}" src="${escape(resource('markdown.js'))}"></script><script nonce="${escape(nonce)}" src="${escape(resource('chat.js'))}"></script></body></html>`;
}
module.exports = {webviewHtml};
