// SPDX-License-Identifier: AGPL-3.0-or-later
(function(root) {
  const escape = value => String(value).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  function inline(text) {
    return text.split(/(`[^`\n]+`)/g).map(part => part.startsWith('`') && part.endsWith('`')
      ? '<code>' + escape(part.slice(1, -1)) + '</code>'
      : escape(part).replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')).join('');
  }
  function renderMarkdown(text) {
    const lines = String(text).replace(/\r\n?/g, '\n').split('\n');
    const blocks = []; let paragraph = [], list = [], kind = '', fence = null, code = [];
    const flush = () => {
      if (paragraph.length) blocks.push('<p>' + paragraph.map(inline).join('<br>') + '</p>');
      if (list.length) blocks.push('<' + kind + '>' + list.map(item => '<li>' + inline(item) + '</li>').join('') + '</' + kind + '>');
      paragraph = []; list = []; kind = '';
    };
    const codeBlock = () => {
      blocks.push('<div class="code-block"><div class="code-header"><span>' + escape(fence.language || 'code') + '</span><button class="copy-code" type="button" aria-label="Copy code">Copy</button></div><pre><code>' + escape(code.join('\n')) + '</code></pre></div>');
      fence = null; code = [];
    };
    for (const line of lines) {
      const marker = /^\s*(`{3,}|~{3,})(.*)$/.exec(line);
      if (fence) {
        if (marker && marker[1][0] === fence.char && marker[1].length >= fence.length && !marker[2].trim()) codeBlock();
        else code.push(line);
      } else if (marker) {
        flush(); fence = {char: marker[1][0], length: marker[1].length, language: marker[2].trim()};
      } else if (!line.trim()) flush();
      else {
        const heading = /^(#{1,6})\s+(.+)$/.exec(line);
        const item = /^\s*(?:([-*+])|\d+[.)])\s+(.+)$/.exec(line);
        if (heading) { flush(); const level = Math.min(4, heading[1].length + 1); blocks.push('<h' + level + '>' + inline(heading[2]) + '</h' + level + '>'); }
        else if (item) {
          const next = item[1] ? 'ul' : 'ol';
          if (paragraph.length || (kind && kind !== next)) flush();
          kind = next; list.push(item[2]);
        } else { if (list.length) flush(); paragraph.push(line); }
      }
    }
    flush(); if (fence) codeBlock();
    return blocks.join('');
  }
  if (typeof module !== 'undefined') module.exports = {renderMarkdown};
  else root.OlithMarkdown = {renderMarkdown};
})(typeof window === 'undefined' ? globalThis : window);
