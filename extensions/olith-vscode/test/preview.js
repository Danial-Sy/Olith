// SPDX-License-Identifier: AGPL-3.0-or-later
// Local visual fixture using the production webview and a simulated host.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {webviewHtml} = require('../webview');
const media = path.join(__dirname, '../media');
const nonce = 'local-preview';
const reply = 'The payment flow starts in `payments/auth.py`.\n\n### What I found\n\n- Tokens are checked before a request reaches the handler.\n- Expired tokens return a clear error.\n\n```python\ndef authenticate(token):\n    claims = verify_token(token)\n    return claims.user_id\n```\n\nI can add a test for the expiry case next.';
const fixture = `(() => {
  const params = new URLSearchParams(location.search);
  const state = {type:'state', cid:'preview', connected:params.get('state') !== 'welcome', project:'my-project', root:'/home/developer/my-project', messages:[], tasks:[], attachments:[], activeFile:'payments/auth.py', running:false, status:'Connected to /home/developer/my-project', models:[{id:'qwen:27b',label:'Qwen 27B'},{id:'coder:30b',label:'Coder 30B'}], modelKey:sessionStorage.getItem('selectedModel') || 'qwen:27b', modelLoading:false};
  const reply = ${JSON.stringify(reply)};
  const events = document.createElement('output'); events.id = 'fixture-events'; events.hidden = true; document.body.append(events);
  let saved = JSON.parse(sessionStorage.getItem('draft') || 'null'), timer;
  const emit = message => window.postMessage(message, location.origin);
  const paint = () => emit(JSON.parse(JSON.stringify(state)));
  const chat = () => { state.messages = [{role:'user',content:'Where are payment tokens authenticated?'},{role:'assistant',content:reply}]; state.tasks = [{status:'completed',content:'Find the authentication flow'},{status:'completed',content:'Check token validation'}]; };
  if (params.get('state') === 'chat') chat();
  if (params.get('state') === 'progress') {
    state.running=true; state.attachments=[{id:'demo-file',path:'payments/auth.py'}];
    state.status='Running pytest tests/test_auth.py';
    state.messages=[{role:'user',content:'Fix expired payment tokens and check the tests.'},
      {role:'assistant',content:'The expiry check is missing. I will add it, then run the focused tests.',activity:[
        {name:'read_file',label:'Reading payments/auth.py',status:'done'},
        {name:'edit_file',label:'Editing payments/auth.py',status:'done'},
        {name:'run_shell',label:'Running pytest tests/test_auth.py',status:'running'}]}];
    state.tasks=[{content:'Inspect token validation',status:'completed'}, {content:'Add the expiry check',status:'completed'}, {content:'Run the authentication tests',activeForm:'Checking token expiry',status:'in_progress'}];
  }
  if (!state.connected) { state.project = ''; state.root = ''; state.status = 'Connect to your local Olith app.'; }
  window.acquireVsCodeApi = () => ({getState:() => saved, setState:value => {saved=value; sessionStorage.setItem('draft', JSON.stringify(value));}, postMessage:message => {
    events.textContent = JSON.stringify(message);
    if (message.action === 'ready') paint();
    if (message.action === 'files' || message.action === 'active-file') { state.attachments=[{id:'demo-file',path:'payments/auth.py'}]; paint(); }
    if (message.action === 'remove-file') { state.attachments=[]; paint(); }
    if (message.action === 'workspace') { state.project='another-project'; state.root='/home/developer/another-project'; state.connected=false; state.attachments=[]; state.status='Pair this folder with Olith'; paint(); }
    if (message.action === 'copy') emit({type:'copied'});
    if (message.action === 'models') { state.status='Installed models refreshed'; paint(); }
    if (message.action === 'model' && state.models.some(model => model.id === message.model)) { state.modelKey=message.model; sessionStorage.setItem('selectedModel', message.model); paint(); }
    if (message.action === 'connect') { state.connected=true; state.project='my-project'; state.root='/home/developer/my-project'; state.status='Connected to /home/developer/my-project'; paint(); }
    if (message.action === 'new') { clearInterval(timer); state.cid += '-new'; state.messages=[]; state.tasks=[]; state.running=false; paint(); }
    if (message.action === 'stop') { clearInterval(timer); state.running=false; state.status='Task stopped'; paint(); }
    if (message.action === 'send') {
      if (message.content === 'cancel') { emit({type:'send-finished'}); return; }
      emit({type:'submitted', content:message.content});
      state.attachments=[]; state.running=true; state.status='Reading payments/auth.py'; state.messages.push({role:'user',content:message.content},{role:'assistant',content:'',activity:[{name:'read_file',label:'Reading payments/auth.py',status:'running'}]});
      state.tasks=[{status:'completed',content:'Find the authentication flow'},{status:'in_progress',content:'Check token validation'}]; paint();
      let index=0; const response=message.content === 'long' ? Array(200).fill(reply).join('\\n\\n') : reply;
      timer=setInterval(() => { state.messages.at(-1).activity[0].status='done'; state.messages.at(-1).content=response.slice(0,index+=message.content === 'long' ? 250 : 12); if(index>=response.length) {clearInterval(timer); state.running=false; state.status='Ready for your next idea'; state.tasks[1].status='completed'; emit({type:'send-finished'});} paint(); }, 100);
    }
  }});
})();`;

http.createServer((request, response) => {
  const url = new URL(request.url, 'http://127.0.0.1:8890');
  if (url.pathname === '/') {
    response.setHeader('Content-Type', 'text/html');
    return response.end('<!doctype html><html><head><title>Olith sidebar preview</title><style>body{margin:0;background:#101114;color:#a8a8b1;font:12px system-ui;padding:24px;display:flex;justify-content:center;gap:24px}section{width:360px}p{letter-spacing:1px;font-size:10px}iframe{width:100%;height:760px;border:1px solid #34343d;border-radius:12px;overflow:hidden}</style></head><body><section><p>OLITH / READY TO CONNECT</p><iframe src="/panel?state=welcome"></iframe></section><section><p>OLITH / LIVE PROGRESS</p><iframe src="/panel?state=progress"></iframe></section><section><p>OLITH / LIGHT THEME</p><iframe src="/panel?state=chat&theme=light"></iframe></section></body></html>');
  }
  if (url.pathname === '/panel') {
    let html = webviewHtml({nonce, source:'http://127.0.0.1:8890', asset:name => '/media/' + name});
    html = html.replace('</head>', '<link rel="stylesheet" href="/theme.css?theme=' + (url.searchParams.get('theme') || 'dark') + '"></head>');
    html = html.replace('<script nonce=', '<script nonce="' + nonce + '" src="/fixture.js"></script><script nonce=');
    response.setHeader('Content-Type', 'text/html'); return response.end(html);
  }
  if (url.pathname === '/fixture.js') { response.setHeader('Content-Type','application/javascript'); return response.end(fixture); }
  if (url.pathname === '/theme.css') {
    response.setHeader('Content-Type','text/css');
    return response.end(':root{--vscode-font-family:system-ui,sans-serif;--vscode-font-size:13px;--vscode-focusBorder:#7d83eb;' + (url.searchParams.get('theme') === 'light' ? '--vscode-sideBar-background:#f7f7f8;--vscode-foreground:#27272c;--vscode-descriptionForeground:#65656f;--vscode-input-background:#ffffff;--vscode-widget-border:#dcdce2;--vscode-textCodeBlock-background:#ededf0;--vscode-input-foreground:#27272c;--vscode-input-placeholderForeground:#71717b;--vscode-textLink-foreground:#345eb6;' : '--vscode-sideBar-background:#18191d;--vscode-foreground:#e4e4e7;--vscode-descriptionForeground:#9d9da7;--vscode-input-background:#222329;--vscode-widget-border:#35363f;--vscode-textCodeBlock-background:#202126;') + '}');
  }
  if (url.pathname.startsWith('/media/') && !url.pathname.slice(7).includes('/')) {
    const name = url.pathname.slice(7);
    if (['chat.css','chat.js','markdown.js','olith.svg'].includes(name)) {
      response.setHeader('Content-Type', name.endsWith('.css') ? 'text/css' : name.endsWith('.svg') ? 'image/svg+xml' : 'application/javascript');
      return response.end(fs.readFileSync(path.join(media,name)));
    }
  }
  response.writeHead(404); response.end();
}).listen(8890, '127.0.0.1', () => process.stdout.write('Olith sidebar fixture: http://127.0.0.1:8890\n'));
