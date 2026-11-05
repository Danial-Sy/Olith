# Olith for VS Code

Use Olith from your editor while the desktop app runs the models and project tools in
the background. You can chat, send selected code, and ask Olith to fix errors reported
by VS Code.

The sidebar uses Olith's logo and colors and follows your VS Code theme. Replies have
formatted text and copyable code blocks. Task progress folds away when you want more
room to read. Press **Enter** to send or **Shift+Enter** for a new line. Your draft
stays put if connecting or saving is cancelled.

Olith shows brief updates and live activity as it reads, edits, and checks your code.
Each step stays visible in the conversation with its result. You can see what is
happening before the final reply, even when a model skips an explanation.

Click **Add files** above the message box to choose source files, or **Use [file]** to
attach the active editor file. Remove an attachment with **×** before sending. The
active file's name follows your editor automatically, so Olith knows which file you mean
by "this file" even without an attachment. Files must be inside the paired workspace.
Olith reads their latest saved text when you send, with up to eight text files, 24,000
characters per file and 48,000 characters total. Larger excerpts are marked as truncated
so the agent can read more with its file tools. Binary files and files over 256 KB are
rejected.

Click the folder name at the top to choose a workspace or open another folder. If that
folder is already paired, Olith reconnects it. A new folder needs its own pairing code.
Switching is disabled during a task so edits stay in the right project.

Pick your model from the dropdown beside the send button after connecting. It uses the
installed models assigned to **Agentic** in Olith's Models panel and remembers the
choice for this chat. Click **↻** to refresh after adding or removing models. The chosen
model loads when you send your next message; switching is disabled while a task runs.
The model needs tool calling and must be assigned to Agentic in Olith. Models without
tool calling and embedding models cannot run Agentic tasks.

## Install and connect

You need VS Code 1.85 or newer and the Olith desktop app on the same computer. The
shortcuts below are for Windows and Linux.

1. Start Olith and open **Connections → VS Code**.
2. Click **Install / update in VS Code** if available, or download the latest
   versioned `olith-vscode` file and follow the manual install steps below.
3. For a manual install, press **Ctrl+Shift+P** in VS Code. This opens the Command
   Palette. If you used the install button, reload VS Code and skip to step 5.
4. Search for **Extensions: Install from VSIX…**, press Enter, and choose the
   latest `olith-vscode` file you downloaded. Run **Developer: Reload Window**
   after installing; this replaces any older panel still running.
5. Use **File → Open Folder…** to open the project you want Olith to work on.
   The extension requires a trusted local workspace. Opening a single file
   without its project folder is not enough.
6. Back in Olith's **VS Code** connection panel, click **Generate pairing code**.
7. In VS Code, press **Ctrl+Shift+P**, run **Olith: Connect to Olith**, and paste
   that code. It works once and expires after two minutes. Olith shows a countdown
   and clears expired or used codes. Click **Generate new code** when needed.
8. Open the **Olith** icon in the left sidebar, or use the Command Palette to run
   **Olith: Open Chat**. Look for **Connected** and the model dropdown beside Send.
   Check the project name at the top; hover it for the full folder path.

Pairing registers the folder as an Olith project if it isn't already there. Keep the
desktop app running while you use the extension. You don't need to install another model
for VS Code. The connection stays active after the pairing code expires.

## Try it out

**Ask about the project.** Type `Explain how this project starts up` in the Olith
sidebar and press **Enter** or click the send arrow. Replies and task progress appear
there.

**Ask for a change.** For example: `Add validation for an empty email address and run
the relevant tests.` Olith works in the paired project. If there are existing unsaved
edits, it asks whether to save them before starting.

**Send selected code.** Highlight a function, right-click it, and choose **Olith: Send
Selection**. Enter a request such as `Explain this function` or `Make this handle an
empty list`. Olith receives the selection and its file location.

**Fix a red squiggle.** Put your cursor on a reported error and press **Ctrl+.** to open
Quick Fix, or click its lightbulb. Choose **Fix with Olith**. Olith reads the
diagnostic, inspects the source, and attempts a fix. VS Code's language support must
report an error for this action to appear. You can also run **Fix with Olith** from the
Command Palette to send the active file's diagnostics.

**Review and stop.** Click the clipboard icon at the top to **Review task changes** and
verification results. The send arrow becomes a **Stop task** button while Olith works.
The **+** button starts a new chat after the task finishes. Questions and approval
prompts appear inside VS Code. Approval modes follow the desktop app's settings.

Olith saves file-tool edits through VS Code, so normal editor undo works. It rejects an
edit if the file changed while it was being prepared or if it would overwrite unsaved
changes. Shell commands, checks, and Git operations run through the backend and appear
as external file updates. Formatting on save can alter a proposed edit; Olith reports
any difference in the saved result.

## If something isn't working

| Problem | What to check |
|---|---|
| Olith commands are missing | Open Extensions with Ctrl+Shift+X and check that Olith is installed and enabled. Reload VS Code if installation asked you to. |
| The workspace isn't trusted | Open a local project folder and use VS Code's Workspace Trust controls for a project you trust. |
| The code expired or was already used | Generate another code in Olith and run Olith: Connect to Olith again. |
| Olith is offline or restarting | Start the desktop app. A saved pairing reconnects automatically. The default server is http://127.0.0.1:7878. |
| Download opens no save dialog | Restart Olith after updating so its desktop download setting is enabled. You can also use olith-vscode.vsix from the release assets. |
| The wrong project is connected | Click the folder name at the top, choose the intended workspace, and pair it if needed. |
| Fix with Olith is missing | Check that the language extension reports a diagnostic, place the cursor on it, and press **Ctrl+.** to open Quick Fix. You can also use the command from the Command Palette. |
| An edit is refused | Save or undo your unsaved changes, then ask Olith to inspect the current source and retry. |

For more detail, open **View → Output** in VS Code and choose **Olith** from the output
channel dropdown.

This version supports trusted local workspaces and UTF-8 files. Multi-root workspaces
pair one folder at a time. Remote, WSL, and SSH workspaces aren't supported yet. Pair
once per workspace. Pairing survives app, editor, and computer restarts without an
inactivity timeout. The token stays in VS Code SecretStorage. Olith saves its hash and
the paired folder. Offline editors are shown as **Paired · Offline** and reconnect
automatically when Olith is available. Run **Olith: Disconnect**, or use **Forget
pairing** in Olith's Connections panel, to remove the pairing. You need to pair again if
you delete the app data, lose the saved VS Code token or remove the pairing.

To update the extension, download the current VSIX from your updated Olith app or use
**Install / update in VS Code** in Connections. After either method, run **Developer:
Reload Window**. The footer shows the running extension version. Restart Olith after
updating the app so the model picker API is available, then reload VS Code after
installing the extension. Older pairings made before restart support was added need to
be paired once more after upgrading. Version 0.1.3 also replaced the old cached H
sidebar icon.

VS Code's documentation also covers [installing a
VSIX](https://code.visualstudio.com/docs/configure/extensions/extension-marketplace#install-from-a-vsix)
and [using Quick Fix](https://code.visualstudio.com/docs/editing/refactoring).

## Development

Build from the Olith repository with `python scripts/package_vscode.py`. Run the client
tests with `npm test --prefix extensions/olith-vscode`.
