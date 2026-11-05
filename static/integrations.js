// SPDX-License-Identifier: AGPL-3.0-or-later
const gitActions = {
  status: ["Status and conflicts"], diff: ["Working diff"], branches: ["Branches"],
  fetch: ["Fetch", "remote"], pull_ff: ["Pull (fast-forward only)", "remote", "branch"], push: ["Push", "remote", "branch"],
  branch: ["Create branch", "branch"], checkout: ["Switch branch", "branch"],
  stage: ["Stage selected files", "paths"], unstage: ["Unstage selected files", "paths"], commit: ["Commit staged changes", "message"],
  merge: ["Merge local branch", "branch"], abort_merge: ["Abort merge"],
  pulls: ["Open pull requests"], pull: ["Read pull request", "number"], checks: ["Pull request checks", "number"],
  create_pull: ["Create draft pull request", "title", "body", "head", "base"],
  update_pull: ["Update pull request", "number", "title", "body", "state"],
  merge_pull: ["Merge reviewed pull request", "number", "sha", "merge_method"],
  issues: ["Open issues"], create_issue: ["Create issue", "title", "body"], comment: ["Comment on issue or pull request", "number", "body"], review: ["Review pull request", "number", "event", "body"],
};
const gitReads = new Set(["status", "diff", "branches", "pulls", "pull", "checks", "issues"]);
const workspaceState = {links: {}, editor: {sessions: []}, github: null, githubChecked: 0, repos: [], reposLoaded: false, repoEpoch: 0, reposBusy: false};
let workspaceRequest = null;
let editorStatusRequest = null;
let githubStatusRequest = null;

async function refreshEditorStatus() {
  if (editorStatusRequest) return editorStatusRequest;
  editorStatusRequest = (async () => {
    try { workspaceState.editor = await api("/api/ide/status"); }
    catch { workspaceState.editor = {sessions: [], available: false}; }
    renderWorkspaceLinks();
    return workspaceState.editor;
  })();
  try { return await editorStatusRequest; } finally { editorStatusRequest = null; }
}

function connectionBadge(element, text, connected) {
  element.textContent = text;
  element.classList.toggle("connected", !!connected);
}

function renderWorkspaceLinks() {
  const sessions = workspaceState.editor.sessions || [];
  const editorFor = project => sessions.filter(session => session.root === project.root);
  const describe = project => {
    const link = workspaceState.links[project.id];
    const paired = editorFor(project), active = paired.some(session => session.connected);
    return {repo: link?.repo || (link?.unavailable ? "Repository unavailable" : link?.git ? "Local Git repository" : link ? "Local folder" : "Repository unavailable"),
      editor: workspaceState.editor.available === false ? "Editor unavailable" : active ? "VS Code connected" : paired.length ? "VS Code offline" : "No editor connected", active, link};
  };
  document.querySelectorAll(".project-item").forEach(card => {
    const project = state.projects.find(project => project.id === card.dataset.id);
    if (!project) return;
    const info = describe(project), selected = $("#projectSelect").value === project.id;
    card.classList.toggle("active", selected);
    card.querySelector(".project-open").setAttribute("aria-pressed", String(selected));
    const repo = card.querySelector(".project-repo"), editor = card.querySelector(".project-editor");
    repo.textContent = info.link?.repo ? `GitHub · ${info.repo}` : info.repo; repo.title = info.link?.repo ? `Linked to GitHub: ${info.repo}${info.link.branch ? ` · ${info.link.branch}` : ""}` : info.repo;
    repo.classList.toggle("linked", !!info.link?.repo);
    editor.textContent = info.editor; editor.classList.toggle("connected", info.active);
  });
  const selected = state.projects.find(project => project.id === $("#projectSelect").value);
  $("#selectedProjectPath").textContent = selected?.root || "Choose a local project to give Olith context.";
  $("#selectedProjectPath").title = selected?.root || "";
  $("#selectedProjectLinks").replaceChildren();
  if (selected) {
    const info = describe(selected);
    for (const [text, live] of [[info.repo, !!info.link?.repo], [info.editor, info.active]]) {
      const badge = document.createElement("span"); badge.textContent = text; badge.className = live ? "connected" : "";
      $("#selectedProjectLinks").append(badge);
    }
  }
  const active = sessions.filter(session => session.connected), unavailable = workspaceState.editor.available === false;
  connectionBadge($("#editorConnectionBadge"), unavailable ? "Unavailable" : active.length ? "Connected" : sessions.length ? "Paired · Offline" : "Not connected", active.length > 0 && !unavailable);
  const outdated = active.some(session => session.version !== workspaceState.editor.extension_version);
  $("#editorConnectionNote").textContent = unavailable ? "Could not check the editor connection. Refresh to try again." : active.length ? `${active.length} editor ${active.length === 1 ? "window is" : "windows are"} connected.${outdated ? " Install the latest extension and reload VS Code for the latest features." : ""}` : sessions.length ? "Your pairing is saved. Reopen VS Code to reconnect." : "Pair an editor to work directly in your files.";
  $("#btnVSCode").textContent = sessions.length ? "Manage VS Code" : "Connect VS Code";
  $("#editorSessions").replaceChildren(...sessions.map(session => {
    const row = document.createElement("div"); row.className = "editor-session";
    const title = document.createElement("strong"), detail = document.createElement("span");
    title.textContent = session.root.split(/[/\\]/).filter(Boolean).pop() || session.root;
    detail.textContent = `${session.connected ? "Connected" : "Offline"} · ${session.version ? `v${session.version}` : "Reload extension"}`;
    const forget = document.createElement("button"); forget.type = "button"; forget.className = "btn-ghost"; forget.textContent = "Forget pairing";
    forget.onclick = async () => {
      forget.disabled = true;
      try { await api("/api/ide/pairings", {method: "DELETE", body: JSON.stringify({root: session.root})}); await refreshEditorStatus(); }
      catch (error) { setStatus(error.message); forget.disabled = false; }
    };
    row.title = session.root; row.append(title, detail, forget); return row;
  }));
  $("#ideConnectionStatus").textContent = unavailable ? "Editor status unavailable." : active.length ? `VS Code connected to ${active.map(session => session.root).join(", ")}${outdated ? ". Update the extension and reload VS Code for the latest features." : "."}` : "No VS Code window is connected yet.";
  if (typeof renderConnectionCatalog === "function") renderConnectionCatalog();
  $("#installIDE").hidden = !workspaceState.editor.can_install;
  const version = workspaceState.editor.extension_version;
  if (version) { $("#downloadIDE").download = `olith-vscode-${version}.vsix`; $("#downloadIDE").textContent = `Download VS Code extension · v${version}`; }
}

function renderGitHubConnection() {
  const account = workspaceState.github, connected = !!account?.connected;
  connectionBadge($("#githubConnectionBadge"), connected ? "Connected" : account?.status === "not_connected" ? "Not connected" : account ? "Needs attention" : "Checking", connected);
  $("#githubConnectionNote").textContent = connected ? `Connected as @${account.login}. Your repositories are available in Projects.` : account?.note || "Checking your GitHub account…";
  $("#btnGitHub").textContent = connected ? "Manage GitHub" : "Connect GitHub";
  $("#projectRepoAccount").textContent = connected ? `@${account.login} · ${workspaceState.repos.length} repositories${workspaceState.reposBusy ? " · loading…" : ""}` : account?.note || "Connect GitHub in Connections to browse your repos.";
  $("#projectConnectGitHub").hidden = connected;
  $("#projectRepoBrowser").hidden = !connected;
  if (typeof renderConnectionCatalog === "function") renderConnectionCatalog();
}

function renderRepositoryList() {
  renderGitHubConnection();
  const query = $("#projectRepoSearch").value.trim().toLowerCase();
  const repos = workspaceState.repos.filter(repo => repo.full_name.toLowerCase().includes(query));
  const populate = (host, items) => {
    host.replaceChildren(...items.map(repo => {
      const row = document.createElement("div"); row.className = "repository-card";
      const title = document.createElement("strong"), meta = document.createElement("span"), button = document.createElement("button");
      title.textContent = repo.full_name; title.title = repo.full_name;
      const local = state.projects.find(project => workspaceState.links[project.id]?.repo?.toLowerCase() === repo.full_name.toLowerCase());
      meta.textContent = `${repo.private ? "Private" : "Public"}${repo.language ? ` · ${repo.language}` : ""}${local ? " · On this computer" : ""}`;
      button.type = "button"; button.className = "repository-action"; button.textContent = local ? "Open" : "Clone";
      button.onclick = () => local ? selectProject(local.id) : cloneGitHub(repo.full_name);
      row.append(title, meta, button); return row;
    }));
    if (!items.length) { const empty = document.createElement("p"); empty.className = "workspace-empty"; empty.textContent = workspaceState.reposBusy ? "Loading repositories…" : query ? "No matching repositories." : "No repositories are available to this account/token."; host.append(empty); }
  };
  populate($("#projectRepoList"), repos);
  if ($("#githubDialog").open) populate($("#githubRepos"), workspaceState.repos);
}

async function loadProjectRepos(force = false) {
  if (!workspaceState.github?.connected || workspaceState.reposBusy || (workspaceState.reposLoaded && !force)) return;
  const epoch = ++workspaceState.repoEpoch;
  workspaceState.reposBusy = true; workspaceState.reposLoaded = false; workspaceState.repos = []; renderRepositoryList();
  try {
    for (let page = 1; page <= 10000; page++) {
      const batch = await api(`/api/github/repos?page=${page}`);
      if (epoch !== workspaceState.repoEpoch) return;
      const seen = new Set(workspaceState.repos.map(repo => repo.full_name.toLowerCase()));
      workspaceState.repos.push(...batch.filter(repo => !seen.has(repo.full_name.toLowerCase())));
      renderRepositoryList();
      if (batch.length < 100) break;
    }
    workspaceState.reposLoaded = true;
    $("#projectRepoStatus").textContent = "";
  } catch (error) { if (epoch === workspaceState.repoEpoch) $("#projectRepoStatus").textContent = error.message; }
  finally { if (epoch === workspaceState.repoEpoch) { workspaceState.reposBusy = false; renderRepositoryList(); } }
}

async function refreshGitHubStatus(force = false) {
  if (githubStatusRequest) { await githubStatusRequest; if (force) return refreshGitHubStatus(true); return; }
  if (!force && Date.now() - workspaceState.githubChecked < 60000) return;
  githubStatusRequest = (async () => {
    let account;
    try { account = await api("/api/github/status"); }
    catch { account = {connected: false, status: "unavailable", note: "Could not check GitHub. Refresh to try again."}; }
    workspaceState.githubChecked = Date.now();
    if (account.login !== workspaceState.github?.login || !account.connected) {
      workspaceState.repoEpoch++; workspaceState.reposBusy = false; workspaceState.reposLoaded = false; workspaceState.repos = [];
    }
    workspaceState.github = account; renderGitHubConnection();
  })();
  try { await githubStatusRequest; } finally { githubStatusRequest = null; }
}

async function refreshWorkspaceStatus({github = false, force = false} = {}) {
  if (!workspaceRequest) {
    workspaceRequest = (async () => {
      const results = await Promise.allSettled([api("/api/project-links"), refreshEditorStatus()]);
      if (results[0].status === "fulfilled") {
        const changed = JSON.stringify(workspaceState.links) !== JSON.stringify(results[0].value);
        workspaceState.links = results[0].value;
        if (changed && workspaceState.reposLoaded) renderRepositoryList();
      } else workspaceState.links = {};
      renderWorkspaceLinks();
    })();
  }
  const pending = workspaceRequest;
  try { await pending; } finally { if (workspaceRequest === pending) workspaceRequest = null; }
  if (github) {
    await refreshGitHubStatus(force);
    if (workspaceState.github?.connected) await loadProjectRepos();
  }
}

async function openProjectGit(project) {
  const dialog = $("#gitDialog");
  $("#gitTitle").textContent = `${project.name} · Git and GitHub`;
  $("#gitSummary").textContent = "Stage specific files before committing. Pull uses fast-forward only. Merge conflicts remain available for review and resolution.";
  const select = $("#gitAction");
  select.replaceChildren(...Object.entries(gitActions).map(([key, [label]]) => new Option(label, key)));
  const renderFields = () => {
    $("#gitFields").replaceChildren();
    for (const key of gitActions[select.value].slice(1)) {
      const label = document.createElement("label"); label.className = "field";
      const span = document.createElement("span");
      span.textContent = ({paths: "Files (one relative path per line)", sha: "Reviewed head SHA", number: "Issue / pull request number", merge_method: "Merge method", head: "Source branch", base: "Target branch"})[key] || key[0].toUpperCase() + key.slice(1);
      let input;
      if (["state", "event", "merge_method"].includes(key)) {
        input = document.createElement("select");
        const options = {state: ["open", "closed"], event: ["COMMENT", "APPROVE", "REQUEST_CHANGES"], merge_method: ["squash", "merge", "rebase"]}[key];
        input.append(...options.map(value => new Option(value, value)));
      } else {
        input = document.createElement(["body", "message", "paths"].includes(key) ? "textarea" : "input");
        if (key === "remote") input.value = "origin";
        if (key === "base") input.value = "main";
        input.required = !["body", "branch"].includes(key) || ["branch", "checkout", "merge"].includes(select.value);
      }
      input.name = key; label.append(span, input); $("#gitFields").append(label);
    }
  };
  select.onchange = renderFields; renderFields();
  $("#closeGit").onclick = () => dialog.close();
  const run = async (action, options = {}) => {
    $("#gitRun").disabled = true;
    $("#gitResult").textContent = "Working…";
    try {
      const result = await api(`/api/projects/${project.id}/git`, {method: "POST", body: JSON.stringify({action, options, confirmed: !gitReads.has(action)})});
      $("#gitResult").textContent = typeof result.output === "string" ? `${result.ok === false ? "Failed\n" : ""}${result.output}` : JSON.stringify(result, null, 2);
      await refreshWorkspaceStatus();
    } catch (error) { $("#gitResult").textContent = error.message; }
    finally { $("#gitRun").disabled = false; }
  };
  $("#gitForm").onsubmit = async e => {
    e.preventDefault(); const action = select.value; const options = Object.fromEntries(new FormData(e.target));
    if (options.paths) options.paths = options.paths.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    if (options.branch === "") delete options.branch;
    if (!gitReads.has(action) && !await confirmAction({title: `${gitActions[action][0]}?`, body: JSON.stringify(options, null, 2) || project.name, ok: gitActions[action][0]})) return;
    await run(action, options);
  };
  dialog.showModal(); await run("status");
}

async function openGitHub() {
  $("#githubDialog").showModal(); $("#githubToken").value = "";
  await Promise.all([refreshGitHub(), refreshConnections()]);
}
async function refreshGitHub() {
  $("#githubAccount").textContent = "Checking GitHub connection…";
  await refreshWorkspaceStatus({github: true, force: true});
  const account = workspaceState.github;
  $("#githubAccount").textContent = account?.connected ? `Connected as @${account.login}` : account?.note || "Not connected";
  await loadProjectRepos(true); renderRepositoryList();
}

async function cloneGitHub(repo = "") {
  if (!repo) repo = await promptEdit({title: "Clone GitHub repository", label: "Repository (owner/name)", value: "", multiline: false});
  if (!repo) return;
  const destination = await promptEdit({title: `Clone ${repo}`, label: "Full path of new local folder", value: "", multiline: false});
  if (!destination) return;
  $("#githubAccount").textContent = "Cloning…";
  $("#projectRepoStatus").textContent = `Cloning ${repo}…`;
  try {
    const result = await api("/api/github/clone", {method: "POST", body: JSON.stringify({repo, destination})});
    $("#githubAccount").textContent = result.ok ? `Cloned to ${result.root}` : result.output;
    $("#projectRepoStatus").textContent = $("#githubAccount").textContent;
    await loadProjects();
    if (result.ok && result.project) await selectProject(result.project.id);
    renderRepositoryList();
  } catch (error) { $("#githubAccount").textContent = error.message; $("#projectRepoStatus").textContent = error.message; }
}
$("#btnGitHub").onclick = openGitHub;
$("#closeGitHub").onclick = () => $("#githubDialog").close();
$("#refreshGitHub").onclick = refreshGitHub;
$("#cloneGitHub").onclick = () => cloneGitHub();
$("#githubConnectForm").onsubmit = async e => {
  e.preventDefault(); const token = $("#githubToken").value; $("#githubToken").value = "";
  try { await api("/api/github/connect", {method: "POST", body: JSON.stringify({token})}); await refreshGitHub(); }
  catch (error) { $("#githubAccount").textContent = error.message; }
};
$("#disconnectGitHub").onclick = async () => {
  try { await api("/api/github/connect", {method: "DELETE"}); await refreshGitHub(); }
  catch (error) { $("#githubAccount").textContent = error.message; }
};
$("#createGitHub").onclick = async () => {
  const name = await promptEdit({title: "Create private GitHub repository", label: "Repository name", value: "", multiline: false});
  if (!name || !await confirmAction({title: "Create private repository?", body: name, ok: "Create repository"})) return;
  try { await api("/api/github/repos", {method: "POST", body: JSON.stringify({action: "create_repo", options: {name, private: true}, confirmed: true})}); await refreshGitHub(); }
  catch (error) { $("#githubAccount").textContent = error.message; }
};
let ideCode = "";
let ideExpiresAt = 0;
let ideCountdown = null;
let ideCodePoll = null;
let ideCodeVersion = 0;

function clearIDECode(message) {
  ideCodeVersion += 1;
  clearInterval(ideCountdown);
  clearInterval(ideCodePoll);
  ideCountdown = ideCodePoll = null;
  ideCode = "";
  $("#idePairCode").textContent = "";
  $("#idePairStatus").textContent = message;
  $("#newIDECode").disabled = false;
}

function updateIDECountdown() {
  const remaining = Math.max(0, Math.ceil((ideExpiresAt - Date.now()) / 1000));
  if (!remaining) {
    clearIDECode("Code expired. Click Generate new code to try again.");
    $("#newIDECode").textContent = "Generate new code";
    return;
  }
  $("#idePairStatus").textContent = `Expires in ${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}. This code works once.`;
}

async function checkIDECode(version) {
  if (!ideCode || version !== ideCodeVersion || !$("#ideDialog").open) return;
  try {
    const result = await api("/api/ide/pair-code/status", {method: "POST", body: JSON.stringify({code: ideCode})});
    if (version !== ideCodeVersion) return;
    if (result.status !== "ready") {
      clearIDECode(result.status === "expired" ? "Code expired. Click Generate new code to try again." : "Code used or replaced. Generate a new code to pair another VS Code window.");
      $("#newIDECode").textContent = "Generate new code";
      if (typeof document !== "undefined") refreshWorkspaceStatus();
    }
  } catch {
    // The local countdown still expires the code during a connection outage.
  }
}

$("#closeIDE").onclick = () => $("#ideDialog").close();
$("#ideDialog").addEventListener("close", () => clearIDECode(""));
$("#downloadIDE").onclick = () => {
  const version = workspaceState.editor.extension_version;
  $("#ideDownloadStatus").textContent = `Save ${version ? `olith-vscode-${version}.vsix` : "olith-vscode.vsix"} if prompted, or check your browser's downloads. Install the newest file, then run Developer: Reload Window in VS Code.`;
};
$("#installIDE").onclick = async () => {
  $("#installIDE").disabled = true; $("#ideDownloadStatus").textContent = "Installing the latest Olith extension…";
  try { const result = await api("/api/ide/install-extension", {method: "POST", body: "{}"}); $("#ideDownloadStatus").textContent = `Olith ${result.version}: ${result.note}`; }
  catch (error) { $("#ideDownloadStatus").textContent = error.message; }
  finally { $("#installIDE").disabled = false; }
};
$("#newIDECode").onclick = async () => {
  clearIDECode("Generating a pairing code...");
  const version = ideCodeVersion;
  $("#newIDECode").disabled = true;
  try {
    const value = await api("/api/ide/pair-code", {method: "POST"});
    if (version !== ideCodeVersion || !$("#ideDialog").open) return;
    ideCode = value.code;
    ideExpiresAt = Date.now() + value.expires_in * 1000;
    $("#idePairCode").textContent = ideCode;
    $("#newIDECode").textContent = "Generate new code";
    updateIDECountdown();
    ideCountdown = setInterval(updateIDECountdown, 250);
    ideCodePoll = setInterval(() => checkIDECode(version), 2000);
  } catch (error) {
    if (version === ideCodeVersion) clearIDECode(error.message);
  } finally {
    if (version === ideCodeVersion) $("#newIDECode").disabled = false;
  }
};
$("#btnVSCode").onclick = () => {
  clearIDECode("Generate a code when you are ready to connect. It expires after two minutes.");
  $("#newIDECode").textContent = "Generate pairing code";
  $("#ideDownloadStatus").textContent = "";
  $("#ideDialog").showModal();
};

$("#projectConnectGitHub").onclick = () => openConnections();
$("#projectCloneOther").onclick = () => cloneGitHub();
$("#projectRepoSearch").oninput = renderRepositoryList;
$("#refreshProjectRepos").onclick = async () => {
  await refreshWorkspaceStatus({github: true, force: true}); await loadProjectRepos(true);
};
$("#refreshIntegrations").onclick = () => Promise.allSettled([refreshConnections(), refreshWorkspaceStatus({github: true, force: true})]);
if (typeof document !== "undefined") {
  refreshWorkspaceStatus({github: true});
  setInterval(() => {
    if (!document.hidden && document.querySelector("#panel-projects.active, #panel-connections.active, #ideDialog[open]")) refreshEditorStatus();
    if (!document.hidden && document.querySelector("#panel-connections.active")) {
      refreshConnections();
      refreshGitHubStatus();
    }
  }, 4000);
}
