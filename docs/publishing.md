# Rename Aether, push Olith, and make a release

The app already uses the Olith name. Follow these steps to rename the GitHub repo, push
the changes and make a release.

## 1. Rename the GitHub repo

Open https://github.com/Danial-Sy/Aether, choose **Settings → General**, change
**Repository name** to **Olith**, then click **Rename**. Your existing history, issues,
stars and releases stay with the repo.

GitHub's guide:
https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository

## 2. Update your local remote

From the current checkout:

```bash
cd /home/danials/aether
git remote set-url origin git@github.com:Danial-Sy/Olith.git
git remote -v
```

That keeps your existing SSH login. If you want HTTPS instead, use
`https://github.com/Danial-Sy/Olith.git` as the remote URL. With HTTPS, enter your
GitHub username and use your classic token when Git asks for a password.

For a classic token, enable **repo** and **workflow**. `repo` covers the repository and
release operations; `workflow` lets you push the changed GitHub Actions file. Olith does
not need admin, account-deletion or package scopes.

Scope reference:
https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps

## 3. Choose the release version

The current version and newest local tag are both **1.5.0**. **1.6.0** is a possible
next version. Pick another unused version if you prefer. Before committing, update both:

- `config.py`: `VERSION = "1.6.0"`
- `olith.json`: `"version": "1.6.0"`

The VS Code extension has its own version in `extensions/olith-vscode/package.json`. The
current extension version is `0.1.6`.

## 4. Commit and push

```bash
git status --short
git add -A
git diff --cached --stat
git commit -m "Rename Aether to Olith and add project tools and integrations"
git push origin main
```

`git add -A` includes the removed Aether filenames and their new Olith replacements. The
ignored `data/`, `.venv/`, and `dist/` folders are left out. If Git rejects the push
because main has moved, fetch and resolve that difference before trying again.

## 5. Publish the release

After your commit is on GitHub, tag that commit:

```bash
git tag -a v1.6.0 -m "Olith 1.6.0"
git push origin v1.6.0
```

Open **GitHub → Actions → Release** and wait for the run to finish. The workflow checks
Python, the extension and the Windows installer, then publishes an `olith-v1.6.0.zip`
source archive and `olith-vscode.vsix`.

Open **Releases → Olith v1.6.0 → Edit** to tidy the title and notes. For example:

> Aether is now Olith. This update adds local code indexing and semantic search,
> automatic project checks, task change review, language-server support, GitHub
> tools, and the first VS Code extension.

Add a short migration note too: the old Aether updater requires `aether.json`, so it
will reject the renamed archive. Existing Aether users should copy the new release's
contents into their installation and rerun **Install-Olith.bat** or `./setup.sh`. Keep
the existing `data/` folder. This preserves their data, installs the new dependencies,
and creates the Olith shortcuts. Later updates can use Olith's updater normally.

The tag workflow creates the release. If it fails, fix the failed Actions step and rerun
it. Never move an already published version tag to different code; use a new version.

## Optional: rename the local folder

The folder can stay `/home/danials/aether`; it doesn't change the app's name. If you
want it renamed too, close Olith first. Python environments and desktop shortcuts
contain absolute paths, so rebuild them after moving the folder:

```bash
cd /home/danials
mv aether Olith
cd Olith
mv .venv ../olith-venv-before-rename
./setup.sh --skip-models
```

Setup creates the new environment and launcher paths. Your existing `data/` folder stays
in place, and model weights do not need to be downloaded again.
