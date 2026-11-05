# Olith

Olith is a local AI desktop app, previously called Aether. The idea is simple: chat,
build things, and work on your projects in one place, with the models running on your
own machine through Ollama.

There are two tabs. **Chat** is for normal conversations, images, and deeper reasoning.
**Agentic** can read and edit your code, run commands, keep a task list, and work
through a project with you.

Model inference and code indexing stay local. GitHub, web search, and browser features
connect to outside services when you use them.

## What's new

### Next release (in progress)

- Aether is now **Olith**, with updated branding and launchers.
- Local semantic code search, cached syntax parsing, and dependency maps help
  the agent find the right code with less digging.
- Test, build, and type-check commands are discovered and saved automatically,
  with manual overrides when you need them.
- Task change review lets you undo selected edits while keeping earlier changes.
- Create projects directly in Olith. The installer prepares an `Olith` folder
  in your home directory for projects made through the app.
- Language-server support adds definitions, references, diagnostics, and rename previews.
- GitHub tools handle repositories, branches, commits, pushes, pulls, and pull requests.
- The VS Code extension brings an Olith-styled sidebar with formatted replies,
  copyable code blocks, a local model picker, live progress, file and folder pickers, selected-code actions, and
  **Fix with Olith** to your editor.
- Connections groups browser sessions, CLIs, service tools, GitHub and VS Code
  under Connected or Disconnected. CLI connections work without a browser login.
- Projects shows linked repos, live editor status and a searchable repo list with cloning.
- Approval modes include Full access, with a warning before you turn it on.
- Connections includes 34 website providers and direct MCP service tools, with
  Railway setup, OAuth sign-in, token storage, and custom hosted or local servers.
- Normal installs include the embedding model, so code search is easier to set up.
- Reliability fixes stop rejected rewrites from turning into delete-and-recreate
  operations, keep finished work from restarting, and make check results clearer.

## What you get

- A coding agent that can work through tasks, with backups and syntax checks.
- Code search that understands what you're looking for, plus cached symbols
  and a dependency map so the agent has a better idea of where to look.
- Automatic discovery of test, build, and type-check commands. You can override
  them under **Projects → Checks**.
- Language-server support for definitions, references, diagnostics, and rename
  previews. Coverage depends on the language and project setup.
- Per-task change review, with selected changes you can undo without resetting Git.
- Git and GitHub tools for repositories, branches, commits, push/pull, merges,
  pull requests, checks, reviews, and issues.
- A VS Code extension with sidebar chat, right-click selection actions, and
  **Fix with Olith** on code errors.
- Images, attachments, optional web search, and browser/computer use.

Computer use is still rough. A model trained for it works much better than a normal
vision model. Code references and rename previews also aren't a guarantee that every
dependency has been found, so tests still matter.

## Create a project

Open **Projects**, click **+**, enter a name, and choose **Create project in Olith**.
Olith creates the folder and selects the project, ready for an Agentic chat. Choose
**Add existing folder** to use a project already on your computer. Removing a project
from Olith keeps its files on disk.

The installer creates `~/Olith` on Linux or `%USERPROFILE%\Olith` on Windows. Running
setup again preserves the projects inside it. Set `OLITH_PROJECTS_ROOT` to use a
different folder for both installation and the app.

## Connect more services

Use **Connections** to search website sign-ins or choose **Connect service tools** for
direct access through MCP. Railway, Notion, Linear, Atlassian, Sentry, Supabase, GitHub
MCP, Vercel, Asana, and Slack have setup presets. Connect your account to discover
available tools; provider app approval may be required. Custom hosted MCP servers and
installed local MCP programs are also supported. Credentials stay in your system
keyring. Service changes ask for approval unless you turn on Full access in Settings.
See [connection setup](docs/connections.md).

## Install

### Windows

1. Download and extract the latest release.
2. Double-click **Install-Olith.bat**.
3. Pick the default models, the recommendation for your PC, or your own model tag.
4. Open Olith from the Desktop or Start Menu shortcut.

The installer sets up Python, Ollama, the app's Python environment, and models. Running
it again is fine; models you already have are reused.

### Ubuntu

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip python3-gi \
  gir1.2-gtk-3.0 gir1.2-webkit2-4.1
curl -fsSL https://ollama.com/install.sh | sh
chmod +x setup.sh olith.sh install-desktop.sh update.sh
./setup.sh
./olith.sh
```

If WebKit 4.1 isn't available, use `gir1.2-webkit2-4.0`. Use `./setup.sh --skip-models`
if you don't want any model downloads.

## Models and code search

The main defaults are Qwen3.8 27B and Qwen3-Coder 30B, tuned around a 24 GB GPU. Smaller
PCs can use smaller models; the installer suggests a setup that fits. You can add Ollama
models and GGUFs from Hugging Face inside the app.

Normal installs also include a small title model and `nomic-embed-text` for code search.
Both run on CPU by default to leave GPU memory for the main model.

Open **Projects → Index → Build semantic index** to prepare code search for a project.
After that, changed files are picked up as you use Olith. The first build can take time;
later updates reuse what is already indexed. Text and symbol search still work with
semantic search turned off.

To add the embedding model to an older install:

```bash
./setup.sh --models embeddings
# Windows: py -3 setup.py --models embeddings
```

If you previously disabled semantic search, turn it back on in Settings.

## GitHub

Open **Connections → GitHub** and connect your token. An existing `gh auth login` also
works. Tokens are kept in your OS keyring and aren't given to the model. You can also
sign in to GitHub in Olith's browser. That saves a website session; repo access uses
your token or CLI login.

Your repos appear under **Projects**, where you can search, clone or open a local copy.
Project cards show the linked repo and whether VS Code is actively connected.
**Connections** shows your GitHub account and editor windows.

Both classic and fine-grained tokens work:

- **Classic:** enable `repo`, or `public_repo` for public repos only. Add
  `workflow` if Olith will push GitHub Actions workflow changes.
- **Fine-grained:** select your repositories, then give Contents, Pull requests,
  and Issues read/write access. Give Checks and Commit statuses read access.
  Add Repository creation write access if you want to create repositories.

Open a project's **···** menu and choose **Git** for local Git and pull-request actions.
Repository changes ask for confirmation. Pulls use fast-forward only, and merge
conflicts stay available to review and fix. Git must be installed and on PATH.

This version supports github.com. Projects loads the repos your token can access,
including accounts with more than 100 repos. You can also clone by owner/name.

## VS Code

1. Start Olith and open **Connections → VS Code**. Use **Install / update in
   VS Code**, or download the latest extension for a manual install.
2. For a manual install, press **Ctrl+Shift+P** in VS Code to open the Command Palette. Search for
   **Extensions: Install from VSIX…**, press Enter, and choose the downloaded file.
   Run **Developer: Reload Window** in VS Code after installing.
3. Choose **File → Open Folder…** and open the project you want Olith to work on.
   The extension needs a trusted local workspace.
4. Back in Olith, click **Generate pairing code**. In VS Code, open the Command
   Palette again, run **Olith: Connect to Olith**, and enter that code within
   two minutes. The panel shows a countdown and clears the code when it expires;
   click **Generate new code** for another. Pairing also adds the folder to
   Olith's projects if needed. The two-minute limit only applies to the code.
5. Open the **Olith** icon in VS Code's left sidebar, or run **Olith: Open Chat**
   from the Command Palette. Check the project name at the top; hover it for the
   full folder path.
6. Try a prompt like **"Explain how this project starts up"**, then send it.

The model dropdown beside Send uses your installed Agentic models from Olith. Your
choice is saved for the chat. Click **↻** to refresh after changing your models.

Click **Add files** above the message box, or use the active-file button, to give Olith
specific source context. Click the folder name at the top to switch projects or open
another folder. New folders need their own pairing. Live updates show what Olith is
reading, editing, and checking as it works.

For selected code, highlight it, right-click, choose **Olith: Send Selection**, and tell
Olith what you want done. For a red squiggle, place your cursor on the error, press
**Ctrl+.**, and choose **Fix with Olith**. VS Code needs a diagnostic from its language
support for that quick fix to appear.

Use the clipboard icon at the top to review task changes and check results. The send
arrow becomes a stop button while Olith works. Approvals appear inside VS Code. The
desktop app stays running in the background. More examples and troubleshooting are in
the [VS Code guide](extensions/olith-vscode/README.md).

File-tool edits go through VS Code, so normal editor undo works. Olith asks before
saving existing unsaved changes and rejects edits if the file changes while it is
preparing them. Shell commands, checks, and Git still change files through the backend.
Local workspaces are supported; remote/WSL/SSH workspaces aren't yet. Pair once per
workspace. Olith remembers it across app and computer restarts, and VS Code reconnects
when the desktop app is available. Use **Olith: Disconnect** to remove a pairing.

## Updates and your data

Run **Update-Olith.bat** on Windows or `./update.sh` on Ubuntu. Chats, projects, memory,
settings, and uploads live in the ignored `data/` folder. Updates preserve that folder
and `.venv/`.

Coming from Aether? Its old updater expects the old app manifest. For the first Olith
upgrade, copy the extracted release's contents into your existing install and run the
new installer. Keep your existing `data/` folder. Later Olith updates use the updater
normally.

Choose an approval mode in Settings. Balanced runs safe commands and asks before risky
commands, GitHub changes and service actions. Shell only skips command approvals. Full
access skips all three, with a warning about the risk. It is off by default. File access
has its own setting.

Integrations use the accounts you connect.

## Development and releases

```bash
python3 setup.py --skip-models
.venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 7878
.venv/bin/python -m unittest discover -s tests
node extensions/olith-vscode/test/client.test.js
```

On Windows, use `py -3` and `.venv\Scripts\python.exe`. Run `python
scripts/package_vscode.py` to build `dist/olith-vscode.vsix`.

For a new release, update `VERSION` in `config.py` and the version in `olith.json`,
commit and push your changes, then push a new version tag. For example, if the version
is 1.6.0:

```bash
git tag -a v1.6.0 -m "Olith 1.6.0"
git push origin v1.6.0
```

The tag starts the release workflow. It runs the checks, then publishes the source ZIP
and VS Code extension. Don't reuse an existing release tag.

See [ARCHITECTURE.md](ARCHITECTURE.md) for implementation details. To measure retrieval
locally, run `.venv/bin/python scripts/bench_repo_index.py`. Measured results are in
[performance notes](docs/performance.md). For the rename, push, and release steps, see
[publishing Olith](docs/publishing.md).

## License

GNU Affero General Public License v3.0. Use it, change it, and share it under the same
license. The full terms are in [LICENSE](LICENSE).

Copyright (C) 2026 Danial Syed
