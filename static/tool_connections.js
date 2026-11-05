// SPDX-License-Identifier: AGPL-3.0-or-later
let toolConnectionData = {connections: [], presets: []};
let toolConnectionEditing = null;
let toolConnectionTimer = null;
let toolConnectionEpoch = 0;

function toolConnectionBadge(entry) {
  const kind = entry.transport === "cli" ? "CLI" : "MCP";
  const ready = entry.enabled && entry.status === "connected";
  const label = ready ? `${kind} tools` : entry.enabled ? `${kind} not ready` : `${kind} paused`;
  const title = ready ? `${kind} tools connected and enabled for tasks` : entry.enabled ? `${kind} tools need verification before tasks can use them` : `${kind} tools disabled for tasks`;
  return `<span class="conn-status conn-access-badge${ready ? " ready" : ""}" title="${title}">${label}</span>`;
}

function serviceToolConnections(service) {
  return toolConnectionData.connections.filter(entry => entry.transport === "cli"
    ? entry.provider === service.id || !!service.tool_preset && entry.id === service.tool_preset
    : entry.id === service.id || !!service.tool_preset && entry.id === service.tool_preset ||
      toolConnectionData.presets.some(p => p.id === service.id && p.url && p.url === entry.url));
}

function serviceToolBadges(service) {
  const matches = serviceToolConnections(service);
  // Show one badge per transport, preferring a usable connection over an older setup.
  return [true, false].map(cli => matches.filter(entry => (entry.transport === "cli") === cli)
    .sort((a, b) => Number(b.enabled) * (b.status === "connected" ? 2 : 1) - Number(a.enabled) * (a.status === "connected" ? 2 : 1))[0])
    .filter(Boolean).map(toolConnectionBadge).join("");
}

function toolConnectionRow(entry) {
  const row = document.createElement("div"); row.className = `conn-row ${entry.status === "connected" && entry.enabled ? "live" : ""}`;
  const heading = document.createElement("div"), icon = document.createElement("span");
  heading.className = "conn-main";
  icon.className = "connection-logo-slot";
  icon.innerHTML = connectionIcon(entry);
  const title = document.createElement("strong"), detail = document.createElement("p"), actions = document.createElement("div");
  title.textContent = entry.label;
  const badges = document.createElement("span");
  badges.className = "conn-badges";
  badges.innerHTML = toolConnectionBadge(entry);
  heading.append(icon, title, badges);
  detail.className = "conn-note";
  detail.textContent = `${entry.status} · ${entry.transport === "cli" ? "CLI" : "MCP"} · ${entry.tool_count} tools · ${entry.enabled ? entry.status === "connected" ? "Ready for tasks" : "Not ready for tasks" : "Paused"}${entry.note ? " · " + entry.note : ""}`;
  actions.className = "conn-actions";
  actions.append(connButton("Manage", () => openToolConnection(entry)),
    connButton(entry.enabled ? "Pause" : "Enable", async () => {
      await api(`/api/tool-connections/${entry.id}`, {method: "PATCH", body: JSON.stringify({enabled: !entry.enabled})});
      await refreshToolConnections();
    }),
    connButton("Forget", async () => {
      if (!await confirmAction({title: "Forget tool connection?", body: `Remove ${entry.label} and its saved credentials from Olith?`, ok: "Forget"})) return;
      await api(`/api/tool-connections/${entry.id}`, {method: "DELETE"});
      await refreshToolConnections();
    }, "danger"));
  row.append(heading, detail, actions);
  return row;
}

let toolConnectionsRequest = null;
async function refreshToolConnections() {
  if (toolConnectionsRequest) return toolConnectionsRequest;
  toolConnectionsRequest = (async () => {
    try {
      toolConnectionData = await api("/api/tool-connections");
    } catch (error) {
      toolConnectionData.connections = [];
      if (typeof connectionMessage === "function") connectionMessage(error.message || "Could not load tool connections.", true);
    }
    if (typeof renderConnectionCatalog === "function") renderConnectionCatalog();
    else {
      const list = $("#toolConnectionList");
      if (list) list.replaceChildren(...toolConnectionData.connections.map(toolConnectionRow));
    }
  })();
  try { await toolConnectionsRequest; } finally { toolConnectionsRequest = null; }
}

function syncToolConnectionFields() {
  const transport = $("#toolConnectionTransport").value;
  const local = transport === "stdio", cli = transport === "cli", http = transport === "http";
  const auth = $("#toolConnectionAuth").value;
  $("#toolConnectionHTTP").hidden = !http;
  $("#toolConnectionLocal").hidden = !local;
  $("#toolConnectionCLI").hidden = !cli;
  $("#toolConnectionProviderField").hidden = toolConnectionData.presets.some(p => p.id === $("#toolConnectionPreset").value && p.transport === "cli");
  $("#toolConnectionProvider").disabled = !cli;
  $("#toolConnectionProvider").required = cli;
  $("#toolConnectionURL").required = http;
  $("#toolConnectionURL").disabled = !http;
  $("#toolConnectionCommand").required = local;
  $("#toolConnectionCommand").disabled = !local;
  $("#toolConnectionTokenField").hidden = !http || auth !== "bearer";
  $("#toolConnectionOAuth").hidden = !http || auth !== "oauth";
  $("#toolConnectionToken").placeholder = toolConnectionEditing?.has_token ? "Leave blank to keep the saved token" : "Saved in your system keyring";
  if (cli) updateCLISetup();
  if (!$("#toolConnectionSave").disabled) $("#toolConnectionSave").textContent = cli ? "Verify CLI connection" : "Save and connect";
}

function updateCLISetup() {
  const preset = toolConnectionData.presets.find(p => p.transport === "cli" && p.provider === $("#toolConnectionProvider").value);
  $("#toolConnectionCLISetup").textContent = preset ? [preset.install, preset.login].map(line => /^(Install |Authenticate )/.test(line) ? `# ${line}` : line).join("\n") : "Choose a CLI provider.";
  const npmInstall = preset?.install?.startsWith("npm install -g ");
  $("#toolConnectionLinuxHelp").hidden = !npmInstall;
  $("#toolConnectionSudoInstall").textContent = npmInstall ? `sudo ${preset.install}` : "";
  if (preset) { $("#toolConnectionDocs").href = preset.docs; $("#toolConnectionDocs").hidden = false; }
}

function applyToolConnectionPreset() {
  const preset = toolConnectionData.presets.find(p => p.id === $("#toolConnectionPreset").value);
  $("#toolConnectionPresetIcon").innerHTML = connectionIcon({id: preset?.provider || preset?.id || toolConnectionEditing?.provider || toolConnectionEditing?.id || ""});
  $("#toolConnectionPresetNote").textContent = preset?.note || "Connect a trusted hosted MCP server or an installed local MCP program.";
  const docs = $("#toolConnectionDocs"); docs.hidden = !preset?.docs;
  if (preset?.docs) docs.href = preset.docs; else docs.removeAttribute("href");
  $("#toolConnectionRailwayCLI").hidden = preset?.id !== "railway";
  $("#toolConnectionSettings").open = !preset || !!preset.requires_client || toolConnectionEditing?.transport === "stdio";
  $("#toolConnectionOAuth").open = !!preset?.requires_client;
  if (preset && !toolConnectionEditing) {
    $("#toolConnectionLabel").value = preset.label;
    $("#toolConnectionId").value = preset.id;
    $("#toolConnectionTransport").value = preset.transport || "http";
    $("#toolConnectionProvider").value = preset.provider || "vercel";
    $("#toolConnectionURL").value = preset.url || "";
    $("#toolConnectionAuth").value = preset.auth;
  }
  syncToolConnectionFields();
  $("#toolConnectionId").oninput();
  $("#toolConnectionForm").scrollTop = 0;
}

function displayToolConnectionStatus(entry) {
  $("#toolConnectionStatus").textContent = `${entry.status}${entry.note ? ": " + entry.note : ""}`;
  const link = $("#toolConnectionAuthorize");
  let url = null;
  try { const parsed = new URL(entry.authorization_url); if (parsed.protocol === "https:" || parsed.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(parsed.hostname)) url = parsed.href; } catch {}
  link.hidden = !url;
  if (url) link.href = url; else link.removeAttribute("href");
  $("#toolConnectionCancel").hidden = !entry.busy;
  $("#toolConnectionSave").disabled = !!entry.busy;
  const cli = $("#toolConnectionTransport").value === "cli";
  $("#toolConnectionSave").textContent = entry.busy ? (url ? "Waiting for sign-in…" : cli ? "Checking CLI login…" : "Connecting…") : cli ? "Verify CLI connection" : entry.status === "connected" ? "Reconnect" : "Save and connect";
}

async function watchToolConnection(cid, epoch) {
  try {
    const status = await api(`/api/tool-connections/${cid}/status`);
    if (epoch !== toolConnectionEpoch || !$("#toolConnectionDialog").open) return;
    displayToolConnectionStatus(status);
    if (status.busy) toolConnectionTimer = setTimeout(() => watchToolConnection(cid, epoch), 1000);
    else { await refreshToolConnections(); }
  } catch (error) {
    if (epoch !== toolConnectionEpoch) return;
    $("#toolConnectionStatus").textContent = error.message;
    $("#toolConnectionSave").disabled = false;
  }
}

async function openToolConnection(entry = null, presetId = "") {
  await refreshToolConnections();
  if (presetId) entry = toolConnectionData.connections.find(c => c.id === presetId) ||
    toolConnectionData.connections.find(c => c.id === presetId.replace(/-cli$/, "") && c.status === "connected") || null;
  const epoch = ++toolConnectionEpoch;
  clearTimeout(toolConnectionTimer);
  $("#toolConnectionForm").reset();
  toolConnectionEditing = entry;
  const preset = $("#toolConnectionPreset");
  preset.replaceChildren(new Option("Custom MCP server", ""), ...toolConnectionData.presets.map(p => new Option(`${p.label}${!p.transport ? " · MCP" : ""}`, p.id)));
  $("#toolConnectionProvider").replaceChildren(...toolConnectionData.presets.filter(p => p.transport === "cli").map(p => new Option(p.label, p.provider)));
  preset.value = entry?.id || presetId;
  preset.disabled = !!entry;
  $("#toolConnectionId").disabled = !!entry;
  $("#toolConnectionStatus").textContent = "";
  $("#toolConnectionSave").disabled = false;
  $("#toolConnectionAuthorize").hidden = true;
  $("#toolConnectionCancel").hidden = true;
  if (entry) {
    for (const [id, value] of Object.entries({Label: entry.label, Id: entry.id, Transport: entry.transport, Provider: entry.provider || "vercel",
      URL: entry.url || "", Auth: entry.auth, Command: entry.command || "", Args: JSON.stringify(entry.args || []),
      ClientId: entry.client_id || "", Scope: entry.scope || ""})) $("#toolConnection" + id).value = value;
    $("#toolConnectionEnabled").checked = entry.enabled;
  }
  $("#toolConnectionEnvNote").textContent = entry?.env_names?.length ? `Saved variables: ${entry.env_names.join(", ")}. Leave blank to keep them; enter {} to clear them.` : "Optional. Values go to your system keyring.";
  applyToolConnectionPreset();
  $("#toolConnectionDialog").showModal();
  if (entry) watchToolConnection(entry.id, epoch);
}

$("#btnAddToolConnection").onclick = () => openToolConnection();
$("#toolConnectionPreset").onchange = applyToolConnectionPreset;
$("#toolConnectionTransport").onchange = syncToolConnectionFields;
$("#toolConnectionAuth").onchange = syncToolConnectionFields;
$("#toolConnectionProvider").onchange = () => {
  if (!toolConnectionEditing) {
    const preset = toolConnectionData.presets.find(p => p.transport === "cli" && p.provider === $("#toolConnectionProvider").value);
    $("#toolConnectionPreset").value = preset?.id || "";
    applyToolConnectionPreset();
  } else updateCLISetup();
};
$("#toolConnectionCopyCLI").onclick = async () => {
  try { await navigator.clipboard.writeText($("#toolConnectionCLISetup").textContent); }
  catch { $("#toolConnectionStatus").textContent = "Select the setup instructions above and copy them into your terminal."; }
};
$("#toolConnectionLabel").oninput = () => {
  if (!toolConnectionEditing) $("#toolConnectionId").value = $("#toolConnectionLabel").value.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64);
  $("#toolConnectionId").oninput();
};
$("#toolConnectionId").oninput = () => {
  const port = new URL(location.href).port || "7878";
  $("#toolConnectionCallback").textContent = `Register this redirect URL if required: http://127.0.0.1:${port}/api/tool-connections/${$("#toolConnectionId").value}/callback`;
};
$("#toolConnectionRailwayCLI").onclick = () => {
  $("#toolConnectionSettings").open = true;
  $("#toolConnectionTransport").value = "stdio";
  $("#toolConnectionCommand").value = "railway";
  $("#toolConnectionArgs").value = '["mcp"]';
  $("#toolConnectionPresetNote").textContent = "Install Railway's official CLI and run railway login first. Olith will reuse that sign-in through railway mcp.";
  syncToolConnectionFields();
};
$("#toolConnectionClose").onclick = () => $("#toolConnectionDialog").close();
$("#toolConnectionDialog").addEventListener("close", () => {
  ++toolConnectionEpoch; clearTimeout(toolConnectionTimer);
  for (const id of ["Token", "ClientSecret", "Env"]) $("#toolConnection" + id).value = "";
});
$("#toolConnectionCancel").onclick = async () => {
  const cid = $("#toolConnectionId").value;
  clearTimeout(toolConnectionTimer);
  const status = await api(`/api/tool-connections/${cid}/cancel`, {method: "POST", body: "{}"});
  displayToolConnectionStatus(status);
  await refreshToolConnections();
};
$("#toolConnectionForm").onsubmit = async event => {
  event.preventDefault();
  const button = $("#toolConnectionSave");
  if (button.disabled || !event.target.reportValidity()) return;
  button.disabled = true;
  const transport = $("#toolConnectionTransport").value;
  const local = transport === "stdio", http = transport === "http";
  let body;
  try {
    const preset = toolConnectionData.presets.find(p => p.id === $("#toolConnectionPreset").value);
    if (preset?.requires_client && http && $("#toolConnectionAuth").value === "oauth" && !$("#toolConnectionClientId").value.trim() && !toolConnectionEditing?.has_oauth) {
      throw new Error(`${preset.label} requires an OAuth client ID${preset.id === "vercel" ? " or HTTPS client metadata URL" : ""}. Follow the provider setup instructions first. Signing into the website does not authorize its tools.`);
    }
    body = {id: $("#toolConnectionId").value.trim(), label: $("#toolConnectionLabel").value.trim(),
      transport, provider: transport === "cli" ? $("#toolConnectionProvider").value : "", auth: http ? $("#toolConnectionAuth").value : "none",
      url: http ? $("#toolConnectionURL").value.trim() : "", command: local ? $("#toolConnectionCommand").value.trim() : "",
      args: local ? JSON.parse($("#toolConnectionArgs").value || "[]") : [],
      enabled: $("#toolConnectionEnabled").checked,
      client_id: http ? $("#toolConnectionClientId").value.trim() : "", scope: http ? $("#toolConnectionScope").value.trim() : "",
      token: http && $("#toolConnectionAuth").value === "bearer" ? $("#toolConnectionToken").value || null : null,
      client_secret: http && $("#toolConnectionAuth").value === "oauth" ? $("#toolConnectionClientSecret").value || null : null};
    if (local && $("#toolConnectionEnv").value.trim()) body.env = JSON.parse($("#toolConnectionEnv").value);
    await api("/api/tool-connections", {method: "POST", body: JSON.stringify(body)});
    toolConnectionEditing = body;
    $("#toolConnectionId").disabled = true;
    $("#toolConnectionPreset").disabled = true;
    const status = await api(`/api/tool-connections/${body.id}/connect`, {method: "POST", body: "{}"});
    for (const id of ["Token", "ClientSecret", "Env"]) $("#toolConnection" + id).value = "";
    displayToolConnectionStatus(status);
    await refreshToolConnections();
    watchToolConnection(body.id, toolConnectionEpoch);
  } catch (error) {
    $("#toolConnectionStatus").textContent = error instanceof SyntaxError ? "Arguments must be a JSON array and environment variables a JSON object." : error.message;
    button.disabled = false;
  }
};
