// SPDX-License-Identifier: AGPL-3.0-or-later
/** Browser sessions stay in Olith's Chrome profile. API keys stay in the OS keyring. */
const browserConnectionWatches = new Map();

function connectionMessage(message, error = false) {
  const activity = $("#connectionActivity");
  if (activity) {
    activity.textContent = message;
    activity.hidden = !message;
    activity.dataset.error = String(error);
  }
  setStatus(message);
}

function syncBrowserConnectionWatches() {
  const pending = (state.connections?.connections || []).filter(c => c.kind === "session" && ["signing in", "confirm sign-in"].includes(c.status));
  const ids = new Set(pending.map(c => c.id));
  for (const [cid, watch] of browserConnectionWatches) {
    if (!ids.has(cid)) { clearTimeout(watch.timer); browserConnectionWatches.delete(cid); }
  }
  for (const connection of pending) {
    if (browserConnectionWatches.has(connection.id)) continue;
    const watch = {};
    browserConnectionWatches.set(connection.id, watch);
    const poll = async () => {
      if (browserConnectionWatches.get(connection.id) !== watch) return;
      try {
        if (!document.hidden) {
          const result = await api(`/api/connections/${connection.id}/status`);
          if (browserConnectionWatches.get(connection.id) !== watch) return;
          const current = state.connections?.connections.find(c => c.id === connection.id);
          if (!current || !["signing in", "confirm sign-in"].includes(current.status)) {
            browserConnectionWatches.delete(connection.id); return;
          }
          const changed = current.status !== result.status || current.status_note !== result.status_note;
          Object.assign(current, result);
          if (changed) renderConnectionCatalog();
          if (!["signing in", "confirm sign-in"].includes(result.status)) {
            browserConnectionWatches.delete(connection.id);
            if (changed) connectionMessage(result.status_note || `${connection.label}: ${result.status}`);
            return;
          }
        }
        watch.timer = setTimeout(poll, 2000);
      } catch (error) {
        if (browserConnectionWatches.get(connection.id) !== watch) return;
        browserConnectionWatches.delete(connection.id);
        connectionMessage(`Could not check ${connection.label}'s browser session: ${error.message}. Use Check session to retry.`, true);
      }
    };
    watch.timer = setTimeout(poll, 2000);
  }
}

async function startBrowserSignIn(connection) {
  connectionMessage(`Opening Olith's browser for ${connection.label}…`);
  const result = await api(`/api/connections/${connection.id}/sign-in`, {method: "POST", body: "{}"});
  await refreshConnections();
  if (result.ok === false) throw new Error(result.error || "Could not open Olith's browser.");
  connectionMessage(result.next || "Sign in in Olith's browser, then return here.");
}

let connectionRefreshRequest = null;
async function refreshConnections() {
  if (connectionRefreshRequest) return connectionRefreshRequest;
  connectionRefreshRequest = (async () => {
    // A failed browser-session request must never prevent CLI/MCP refreshes.
    await Promise.allSettled([
      (async () => {
        try {
          const data = await api("/api/connections");
          state.connections = data;
          syncBrowserConnectionWatches();
          const foot = $("#connFoot");
          if (foot) foot.textContent = data.keyring.ok
            ? "Sign-ins are kept as sessions in Olith's browser. Keys go to your system keyring."
            : `Keys cannot be stored here: ${data.keyring.backend}. Browser sign-ins still work.`;
        } catch (error) {
          connectionMessage(error.message || "Could not load browser connections.", true);
        }
      })(),
      typeof refreshToolConnections === "function" ? refreshToolConnections() : Promise.resolve(),
    ]);
    renderConnectionCatalog();
  })();
  try { await connectionRefreshRequest; } finally { connectionRefreshRequest = null; }
}

let connectionCatalogSnapshot = "";
function renderConnectionCatalog() {
  const data = state.connections || {connections: [], available: []};
  const list = $("#connList"), available = $("#connAvailable");
  if (!list || !available) return;
  const hasWorkspace = typeof workspaceState !== "undefined";
  const tools = typeof toolConnectionData !== "undefined" ? toolConnectionData : {connections: [], presets: []};
  const snapshot = JSON.stringify([data, tools, hasWorkspace ? [workspaceState.github, workspaceState.editor] : null,
    $("#connectionSearch").value, $("#connectionCategory").value]);
  // Background polling must not replace focused buttons or open category menus
  // when none of the connection state has changed.
  if (snapshot === connectionCatalogSnapshot) return;
  connectionCatalogSnapshot = snapshot;
  const services = new Map();
  for (const c of [...data.available, ...data.connections]) {
    if (c.id !== "github-api") services.set(c.id, {...c, saved: data.connections.some(entry => entry.id === c.id), tools: []});
  }
  if (hasWorkspace) {
    services.set("github", {...services.get("github"), id: "github", label: "GitHub", category: "Development", tools: []});
    services.set("vscode", {id: "vscode", label: "VS Code", category: "Development", tools: []});
  }
  // Include tool-only services and custom servers, even without a browser login.
  for (const entry of tools.connections) {
    let service = [...services.values()].find(c => serviceToolConnections(c).includes(entry));
    if (!service) {
      const id = entry.transport === "cli" ? entry.provider : entry.id;
      if (!services.has(id)) services.set(id, {id, label: entry.label.replace(/ CLI$/, ""), category: "Service tools", tools: []});
      service = services.get(id);
    }
    service.tools.push(entry);
  }
  for (const preset of tools.presets) {
    if ([...services.values()].some(c => c.id === (preset.provider || preset.id) || c.tool_preset === preset.id || c.tools.some(t => t.id === preset.id))) continue;
    const id = preset.provider || preset.id;
    services.set(id, {id, label: preset.label.replace(/ CLI$/, ""), category: preset.category || "Service tools", note: preset.note, tool_preset: preset.id, tools: []});
  }
  const query = ($("#connectionSearch").value || "").trim().toLowerCase();
  const category = $("#connectionCategory"), selected = category.value;
  const categories = [...new Set([...services.values()].map(c => c.category || "Accounts"))].sort();
  category.replaceChildren(new Option("All categories", ""), ...categories.map(c => new Option(c, c)));
  category.value = categories.includes(selected) ? selected : "";
  // Keep the live GitHub/editor nodes (and their event handlers) when moving groups.
  const githubCard = hasWorkspace ? $("#githubConnectionCard") : null;
  const editorCard = hasWorkspace ? $("#editorConnectionCard") : null;
  list.replaceChildren(); available.replaceChildren();
  let connectedCount = 0, disconnectedCount = 0;
  for (const c of [...services.values()].sort((a, b) => a.label.localeCompare(b.label))) {
    const matches = (!category.value || (c.category || "Accounts") === category.value)
      && `${c.label} ${c.note || ""} ${c.category || ""} ${c.tools.map(t => `${t.label} ${t.transport}`).join(" ")}`.toLowerCase().includes(query);
    const ready = c.status === "connected" || c.tools.some(t => t.enabled && t.status === "connected")
      || hasWorkspace && (c.id === "github" && workspaceState.github?.connected
        || c.id === "vscode" && workspaceState.editor.available !== false && workspaceState.editor.sessions?.some(session => session.connected));
    let row;
    if (hasWorkspace && ["github", "vscode"].includes(c.id)) {
      row = c.id === "github" ? githubCard : editorCard;
      let access = row.querySelector(".service-extra-access");
      if (!access) { access = document.createElement("div"); access.className = "service-extra-access"; row.appendChild(access); }
      access.replaceChildren();
      if (c.id === "github" && c.kind) access.appendChild(c.saved ? connectionRow(c) : offerRow(c));
      c.tools.forEach(entry => access.appendChild(toolConnectionRow(entry)));
    } else {
      row = c.kind ? (c.saved ? connectionRow(c) : offerRow(c)) : document.createElement("div");
      if (!c.kind) {
        row.className = "conn-row";
        row.innerHTML = `<div class="conn-main">${connectionIcon(c)}<span class="conn-name">${escapeHtml(c.label)}</span></div>`;
        if (c.tool_preset && !c.tools.length) row.appendChild(connButton("Connect tools", () => openToolConnection(null, c.tool_preset)));
      }
      c.tools.forEach(entry => row.appendChild(toolConnectionRow(entry)));
    }
    row.dataset.service = c.id;
    row.hidden = !matches;
    row.className = row.className.split(" ").filter(name => name !== "live").join(" ");
    if (ready) { row.className += " live"; list.appendChild(row); if (matches) connectedCount++; }
    else { available.appendChild(row); if (matches) disconnectedCount++; }
  }
  $("#connConnectedCount").textContent = `Connected · ${connectedCount}`;
  $("#connCatalogCount").textContent = `Disconnected · ${disconnectedCount}`;
  for (const [host, count, text] of [[list, connectedCount, "No matching connected services."], [available, disconnectedCount, "No matching disconnected services."]]) {
    if (!count) { const empty = document.createElement("p"); empty.className = "conn-empty"; empty.textContent = text; host.appendChild(empty); }
  }
  const githubBrowser = $("#githubBrowserConnection");
  if (githubBrowser) {
    githubBrowser.replaceChildren();
    const saved = data.connections.find(c => c.id === "github"), offered = data.available.find(c => c.id === "github");
    if (saved) githubBrowser.appendChild(connectionRow(saved));
    else if (offered) githubBrowser.appendChild(offerRow(offered));
  }
}

function connectionRow(c) {
  const row = document.createElement("div");
  row.className = `conn-row ${c.status === "connected" ? "live" : ""}`;
  row.innerHTML = `
    <div class="conn-main">
      ${connectionIcon(c)}
      <span class="conn-name">${escapeHtml(c.label)}</span>
      <span class="conn-badges">${typeof serviceToolBadges === "function" ? serviceToolBadges(c) : ""}<span class="conn-status">${escapeHtml(c.kind === "session" && c.status === "connected" ? "Browser session" : c.kind === "session" && c.status === "confirm sign-in" ? "Confirm browser session" : c.kind === "session" ? `Browser: ${c.status}` : c.status)}</span></span>
    </div>
    <div class="conn-note">${escapeHtml(c.category || "Accounts")} · ${c.kind === "session" ? "Browser access only" : "Saved key"}<br>${escapeHtml(c.note || "")}</div>
    ${c.status_note ? `<p class="conn-feedback">${escapeHtml(c.status_note)}</p>` : c.kind === "session" && c.status === "connected" ? '<p class="conn-feedback">Website session saved for computer use. Service tools connect separately.</p>' : ""}`;
  const actions = document.createElement("div");
  actions.className = "conn-actions";
  if (c.kind === "session") {
    if (c.tool_preset) actions.appendChild(connButton("Connect tools", () => openToolConnection(null, c.tool_preset)));
    actions.appendChild(connButton(c.status === "connected" ? "Sign in again" : "Browser sign-in", () => startBrowserSignIn(c), "", "Opening browser…"));
    actions.appendChild(connButton("Check session", async () => {
      connectionMessage(`Checking ${c.label}'s browser session…`);
      const r = await api(`/api/connections/${c.id}/check`, { method: "POST", body: "{}" });
      await refreshConnections();
      if (r.ok === false) throw new Error(r.error || "Could not check the browser session.");
      connectionMessage(r.status_note || `${c.label}: ${r.status}`);
    }, "", "Checking session…"));
    if (c.status === "confirm sign-in") actions.appendChild(connButton("Confirm browser session", async () => {
      await api(`/api/connections/${c.id}/confirm`, {method: "POST", body: "{}"});
      await refreshConnections();
      connectionMessage(`${c.label} browser session confirmed. Direct service tools connect separately.`);
    }));
  } else {
    actions.appendChild(connButton(c.has_secret ? "Replace key" : "Add key", () => askForKey(c.id, c.label)));
  }
  actions.appendChild(connButton("Forget", async () => {
    await api(`/api/connections/${c.id}`, { method: "DELETE" });
    await refreshConnections();
  }, "danger"));
  row.appendChild(actions);
  return row;
}

function offerRow(c) {
  const row = document.createElement("div");
  row.className = "conn-row offer";
  row.innerHTML = `
    <div class="conn-main">${connectionIcon(c)}<span class="conn-name">${escapeHtml(c.label)}</span><span class="conn-badges">${typeof serviceToolBadges === "function" ? serviceToolBadges(c) : ""}</span></div>
    <div class="conn-note">${escapeHtml(c.note || "")}</div>`;
  const actions = document.createElement("div");
  actions.className = "conn-actions";
  if (c.tool_preset) actions.appendChild(connButton("Connect tools", () => openToolConnection(null, c.tool_preset)));
  if (c.kind) actions.appendChild(connButton(c.kind === "session" ? "Browser sign-in" : "Add key", async () => {
    await api("/api/connections", { method: "POST", body: JSON.stringify({ id: c.id }) });
    if (c.kind === "session") {
      await startBrowserSignIn(c);
    } else {
      askForKey(c.id, c.label);
    }
    await refreshConnections();
  }, "", c.kind === "session" ? "Opening browser…" : "Working…"));
  row.appendChild(actions);
  return row;
}

function connButton(label, onClick, extra = "", busyLabel = "Working…") {
  const b = document.createElement("button");
  b.type = "button";
  b.className = `conn-btn ${extra}`;
  b.textContent = label;
  b.onclick = async () => {
    b.disabled = true;
    b.textContent = busyLabel;
    b.setAttribute("aria-busy", "true");
    try {
      await onClick();
    } catch (e) {
      connectionMessage(e.message || "That did not work", true);
    } finally {
      b.disabled = false;
      b.textContent = label;
      b.removeAttribute("aria-busy");
    }
  };
  return b;
}

async function askForKey(id, label) {
  // Typed once, sent straight to the keyring, never read back into the app.
  const value = prompt(`Key or token for ${label}. It goes to your system keyring, not to a file here.`);
  if (!value) return;
  try {
    await api(`/api/connections/${id}/secret`, {
      method: "POST",
      body: JSON.stringify({ value }),
    });
    setStatus(`${label} key stored`);
  } catch (e) {
    setStatus(e.message || "Could not store that key");
  }
  await refreshConnections();
}

