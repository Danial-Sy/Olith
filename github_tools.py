# SPDX-License-Identifier: AGPL-3.0-or-later
"""Direct Git/GitHub operations. Credentials never enter tool arguments or logs."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import connections

READ_ACTIONS = frozenset({"status", "diff", "branches", "repos", "pulls", "pull", "checks", "issues"})
LOCAL_ACTIONS = frozenset({"stage", "unstage", "commit", "branch", "checkout", "merge", "abort_merge", "fetch", "pull_ff", "push"})
REMOTE_ACTIONS = frozenset({"create_repo", "create_pull", "update_pull", "merge_pull", "create_issue", "comment", "review"})
ACTIONS = READ_ACTIONS | LOCAL_ACTIONS | REMOTE_ACTIONS


def token() -> str | None:
    value = connections.secret("github-api")
    if value:
        return value
    if shutil.which("gh"):
        result = subprocess.run(["gh", "auth", "token", "--hostname", "github.com"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return result.stdout.strip() or None
    return None


def api(method: str, endpoint: str, body: dict | None = None):
    credential = token()
    if not credential:
        raise ValueError("Connect GitHub API in Olith, or run gh auth login first.")
    # Endpoints are constructed here, never supplied by a model or redirect.
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        response = client.request(method, "https://api.github.com" + endpoint, json=body,
                                  headers={"Authorization": "Bearer " + credential,
                                           "Accept": "application/vnd.github+json",
                                           "X-GitHub-Api-Version": "2026-03-10"})
    if not response.is_success:
        try:
            message = str(response.json().get("message", "Request failed"))
        except ValueError:
            message = "Request failed"
        raise ValueError(f"GitHub {response.status_code}: {message.replace(credential, '[redacted]')[:500]}")
    return response.json() if response.content else {"ok": True}


def account() -> dict:
    user = api("GET", "/user")
    return {"login": user["login"], "url": user["html_url"], "connected": True}


def connect(value: str) -> dict:
    # Verify before replacing the working key. No plaintext fallback.
    if not value.strip() or len(value) > 1000:
        raise ValueError("Enter a GitHub access token")
    ok, why = connections.keyring_backend()
    if not ok:
        raise ValueError(why + "; use gh auth login with a system credential store instead")
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        result = client.get("https://api.github.com/user", headers={"Authorization": "Bearer " + value.strip()})
    if result.status_code != 200:
        raise ValueError("GitHub rejected this token")
    connections.store_secret("github-api", value.strip())
    return {"login": result.json()["login"], "connected": True}


def _run(root: Path, args: list[str], *, auth=False, timeout=60, check=True) -> dict:
    binary = shutil.which("git")
    if not binary:
        raise ValueError("Git is not installed or on PATH. Install Git, restart Olith, and retry.")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    credential = token() if auth else None
    with tempfile.TemporaryDirectory(prefix="olith-git-") as temp:
        if credential:
            # Keep credentials off argv and disk; only answer GitHub prompts.
            helper = Path(temp) / ("askpass.cmd" if os.name == "nt" else "askpass.sh")
            script = Path(temp) / "askpass.py"
            script.write_text("import os,re,sys\nfrom urllib.parse import urlsplit\nprompt=sys.argv[1]\nmatch=re.search(r'https://[^\\s\\\"\\\']+', prompt)\nurl=urlsplit(match[0].rstrip(\":'\\\"\")) if match else None\nif not url or url.hostname != 'github.com': sys.exit(1)\nprint('x-access-token' if 'username' in prompt.lower() else os.environ['OLITH_GIT_TOKEN'])\n")
            if os.name == "nt":
                helper.write_text(f'@echo off\n"{sys.executable}" "{script}" %*\n')
            else:
                import shlex
                helper.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(script)) + " \"$@\"\n")
                helper.chmod(0o700)
            env.update(GIT_ASKPASS=str(helper), OLITH_GIT_TOKEN=credential)
        helper_options = ["-c", "credential.helper="] if credential else []
        result = subprocess.run([binary, "--literal-pathspecs", "-c", "credential.interactive=false", *helper_options, *args], cwd=root,
                                env=env, capture_output=True, text=True, timeout=timeout)
    output = ((result.stdout or "") + (result.stderr or ""))[-24000:]
    output = re.sub(r"(https?://)[^/\s@]+@", r"\1[redacted]@", output)
    if credential:
        output = output.replace(credential, "[redacted]")
    if check and result.returncode:
        raise ValueError(output.strip() or f"Git exited {result.returncode}")
    return {"ok": result.returncode == 0, "returncode": result.returncode, "output": output.strip()}


def _ref(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}", value) or any(x in value for x in ("..", "//", ".lock")) or value.endswith(("/", ".")):
        raise ValueError("Invalid Git branch or remote name")
    return value


def _repo(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value or "") or any(p in {".", ".."} for p in value.split("/")):
        raise ValueError("Repository must be owner/name on github.com")
    return value


def repository(root: Path) -> str:
    remote = _run(root, ["remote", "get-url", "origin"])["output"]
    match = re.fullmatch(r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?", remote)
    if match:
        return _repo(match[1])
    url = urlsplit(remote)
    if url.scheme == "https" and url.hostname == "github.com" and not url.username and not url.password:
        return _repo(url.path.strip("/").removesuffix(".git"))
    raise ValueError("Origin is not a supported github.com remote")


def _paths(root: Path, values) -> list[str]:
    if not isinstance(values, list) or not values or len(values) > 200:
        raise ValueError("Select 1–200 explicit files")
    out = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("Select explicit relative file paths")
        path = root / str(value)
        if not str(value) or Path(value).is_absolute() or path.resolve() == root or not path.resolve().is_relative_to(root) or ".git" in Path(value).parts or path.is_dir():
            raise ValueError("File must be inside the project")
        out.append(str(Path(value)))
    return out


def _own_root(root: Path) -> Path:
    """The project must be the repository, not a folder inside one."""
    root = root.resolve()
    top = Path(_run(root, ["rev-parse", "--show-toplevel"])["output"]).resolve()
    if top != root:
        raise ValueError(f"This project is inside the repository at {top}, not at its root. Run git init -b main here to give it its own repository, or open {top} as the project.")
    return root


def _publishable(root: Path | None) -> tuple[Path, str]:
    """Checked before the repository exists, so a refusal leaves nothing on GitHub."""
    if root is None:
        raise ValueError("Attach a project first")
    root = _own_root(root)
    if not _run(root, ["rev-parse", "--verify", "--quiet", "HEAD"], check=False)["ok"]:
        raise ValueError("Nothing to publish yet. Commit the project first.")
    if _run(root, ["remote", "get-url", "origin"], check=False)["ok"]:
        raise ValueError("This repository already has an origin. Use action push, or remove the remote first.")
    return root, _run(root, ["symbolic-ref", "--short", "HEAD"])["output"]


def execute(root: Path | None, action: str = "status", **args) -> dict | list:
    if action not in ACTIONS:
        raise ValueError("Unsupported repository action")
    if action == "repos":
        page = args.get("page", 1)
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 10000:
            raise ValueError("Invalid repository page")
        return api("GET", f"/user/repos?per_page=100&sort=updated&affiliation=owner,collaborator,organization_member&page={page}")
    if action == "create_repo":
        name = _repo("owner/" + str(args.get("name", ""))).split("/")[1]
        publish = args.get("publish") in (True, "true")
        if publish:
            root, branch = _publishable(root)
        repo = api("POST", "/user/repos", {"name": name, "description": str(args.get("description", ""))[:1000], "private": bool(args.get("private", True)), "auto_init": bool(args.get("auto_init", False))})
        result = {key: repo.get(key) for key in ("full_name", "html_url", "clone_url", "private", "default_branch")}
        if publish:
            _run(root, ["remote", "add", "origin", repo["clone_url"]])
            result["push"] = _run(root, ["push", "--set-upstream", "origin", branch], auth=True, timeout=120, check=False)
        return result
    if root is None:
        raise ValueError("Attach a project first")
    root = _own_root(root)
    if action == "status":
        return {"status": _run(root, ["status", "-sb"])["output"], "log": _run(root, ["log", "-8", "--oneline"], check=False)["output"],
                "remotes": _run(root, ["remote", "-v"])["output"], "conflicts": _run(root, ["diff", "--name-only", "--diff-filter=U"])["output"]}
    if action == "diff":
        return _run(root, ["diff", *(["--cached"] if args.get("staged") else []), "--stat", "--patch", "--"])
    if action == "branches":
        return _run(root, ["branch", "-avv"])
    if action in LOCAL_ACTIONS:
        remote = _ref(args.get("remote") or "origin")
        branch = _ref(args.get("branch") or _run(root, ["symbolic-ref", "--short", "HEAD"])["output"])
        if action in {"checkout", "merge", "pull_ff"} and _run(root, ["status", "--porcelain"])["output"]:
            raise ValueError("Commit or stash working changes before switching, pulling, or merging")
        command = {
            "fetch": ["fetch", "--prune", remote], "pull_ff": ["pull", "--ff-only", remote, branch],
            "push": ["push", "--set-upstream", remote, branch], "branch": ["switch", "-c", branch],
            "checkout": ["switch", branch], "merge": ["merge", "--no-edit", "--no-ff", branch],
            "abort_merge": ["merge", "--abort"],
        }.get(action)
        if action in {"stage", "unstage"}:
            command = (["add", "--"] if action == "stage" else ["restore", "--staged", "--"]) + _paths(root, args.get("paths"))
        if action == "commit":
            message = str(args.get("message") or "").strip()
            if not message or len(message) > 10000:
                raise ValueError("A commit message is required")
            command = ["commit", "-m", message]
        return _run(root, command, auth=action in {"fetch", "pull_ff", "push"}, timeout=120, check=False)
    repo = _repo(args.get("repo") or repository(root))
    prefix = "/repos/" + repo
    if action in {"pulls", "issues"}:
        return api("GET", prefix + "/" + action + "?state=open&per_page=100")
    if action in {"pull", "checks", "update_pull", "merge_pull", "comment", "review"}:
        number = args.get("number")
        if isinstance(number, bool) or not str(number).isdigit() or not 1 <= int(number) <= 10000000:
            raise ValueError("A positive pull request or issue number is required")
        pull = prefix + "/pulls/" + str(int(number))
    if action == "pull":
        detail = api("GET", pull)
        return {**detail, "files": api("GET", pull + "/files?per_page=100"),
                "reviews": api("GET", pull + "/reviews?per_page=100"),
                "note": "Files/reviews are limited to 100 per request. Fetch the branch for complete local inspection of larger changes."}
    if action == "checks":
        sha = api("GET", pull)["head"]["sha"]
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise ValueError("Invalid head revision")
        return {"checks": api("GET", prefix + "/commits/" + sha + "/check-runs?per_page=100"), "status": api("GET", prefix + "/commits/" + sha + "/status")}
    if action == "create_pull":
        head = str(args.get("head") or "")
        if ":" in head:
            owner, branch = head.split(":", 1)
            if not re.fullmatch(r"[A-Za-z0-9-]+", owner):
                raise ValueError("Invalid pull request source owner")
            head = owner + ":" + _ref(branch)
        else:
            head = _ref(head)
        return api("POST", prefix + "/pulls", {"title": str(args.get("title") or ""), "body": str(args.get("body") or ""), "head": head, "base": _ref(args.get("base") or "main"), "draft": bool(args.get("draft", True))})
    if action == "update_pull":
        fields = {k: args[k] for k in ("title", "body", "state", "base") if k in args}
        if fields.get("state", "open") not in {"open", "closed"}:
            raise ValueError("Invalid pull request state")
        return api("PATCH", pull, fields)
    if action == "merge_pull":
        sha = str(args.get("sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise ValueError("Review the pull request first, then supply its exact head SHA")
        method = args.get("merge_method", "squash")
        if method not in {"merge", "squash", "rebase"}:
            raise ValueError("Invalid merge method")
        return api("PUT", pull + "/merge", {"sha": sha, "merge_method": method})
    if action == "create_issue":
        return api("POST", prefix + "/issues", {"title": str(args.get("title") or ""), "body": str(args.get("body") or "")})
    if action == "comment":
        return api("POST", prefix + f"/issues/{int(number)}/comments", {"body": str(args.get("body") or "")})
    if action == "review":
        event = args.get("event", "COMMENT")
        if event not in {"COMMENT", "APPROVE", "REQUEST_CHANGES"}:
            raise ValueError("Invalid review event")
        return api("POST", pull + "/reviews", {"event": event, "body": str(args.get("body") or "")})
    raise ValueError("Unsupported action")


def clone(repo: str, destination: str) -> dict:
    repo = _repo(repo)
    path = Path(destination).expanduser().absolute()
    if not path.parent.is_dir() or path.exists():
        raise ValueError("Choose a new folder under an existing parent directory")
    result = _run(path.parent, ["clone", "--", "https://github.com/" + repo + ".git", str(path)], auth=True, timeout=180, check=False)
    return {**result, "root": str(path)}
