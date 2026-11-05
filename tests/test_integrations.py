# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
import hashlib
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
import agent
import editor_bridge as bridge
import github_tools as git
import server
import storage
import integrations
from scripts.package_vscode import package


class GitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "repo"; self.root.mkdir()
        self.run_git("init", "-b", "main")
        self.run_git("config", "user.email", "test@example.invalid")
        self.run_git("config", "user.name", "Integration Test")
        self.run_git("config", "commit.gpgsign", "false")
        (self.root / "app.py").write_text("x = 1\n")
        self.run_git("add", "app.py"); self.run_git("commit", "-m", "Initial")
        self.patcher = patch.object(git, "token", return_value=None)
        self.patcher.start(); self.addCleanup(self.patcher.stop)

    def run_git(self, *args):
        result = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_stage_commit_branch_merge_and_specific_paths(self):
        git.execute(self.root, "branch", branch="feature/payments")
        (self.root / "app.py").write_text("x = 2\n")
        (self.root / "keep.txt").write_text("user's uncommitted work\n")
        git.execute(self.root, "stage", paths=["app.py"])
        self.assertTrue(git.execute(self.root, "commit", message="Fix payment")['ok'])
        self.assertEqual(self.run_git("diff", "--cached"), "")
        self.assertIn("keep.txt", git.execute(self.root)["status"])
        with self.assertRaisesRegex(ValueError, "working changes"):
            git.execute(self.root, "checkout", branch="main")
        (self.root / "keep.txt").unlink()
        git.execute(self.root, "checkout", branch="main")
        self.assertTrue(git.execute(self.root, "merge", branch="feature/payments")["ok"])
        self.assertEqual((self.root / "app.py").read_text(), "x = 2\n")

    def fake_github(self):
        bare = self.base / "new.git"
        subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True, check=True)
        # Everything GitHub sends that the model does not need stays out of its context.
        reply = {"full_name": "me/new", "html_url": "https://github.com/me/new", "clone_url": str(bare),
                 "private": True, "default_branch": "main", "forks_url": "https://api.github.com/noise"}
        return bare, patch.object(git, "api", return_value=reply)

    def test_create_repo_publish_adds_origin_and_pushes_in_one_call(self):
        bare, api = self.fake_github()
        with api as call:
            result = git.execute(self.root, "create_repo", name="new", publish=True)
        call.assert_called_once()
        self.assertTrue(result["push"]["ok"])
        self.assertNotIn("forks_url", result)
        self.assertEqual(self.run_git("remote", "get-url", "origin"), str(bare))
        self.assertEqual(self.run_git("rev-parse", "--abbrev-ref", "main@{upstream}"), "origin/main")
        pushed = subprocess.run(["git", "rev-parse", "main"], cwd=bare, capture_output=True, text=True).stdout.strip()
        self.assertEqual(pushed, self.run_git("rev-parse", "HEAD"))

    def test_create_repo_without_publish_leaves_the_project_alone(self):
        _, api = self.fake_github()
        with api:
            result = git.execute(self.root, "create_repo", name="new")
        self.assertNotIn("push", result)
        self.assertEqual(self.run_git("remote"), "")

    def test_publish_refusals_leave_no_empty_repo_on_github(self):
        _, api = self.fake_github()
        with api as call:
            self.run_git("remote", "add", "origin", "https://github.com/me/old.git")
            with self.assertRaisesRegex(ValueError, "already has an origin"):
                git.execute(self.root, "create_repo", name="new", publish=True)
            self.run_git("remote", "remove", "origin")
            nested = self.root / "sub"; nested.mkdir()
            with self.assertRaisesRegex(ValueError, "git init -b main"):
                git.execute(nested, "create_repo", name="new", publish=True)
            empty = self.base / "empty"; empty.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=empty, capture_output=True, check=True)
            with self.assertRaisesRegex(ValueError, "Commit the project first"):
                git.execute(empty, "create_repo", name="new", publish=True)
            with self.assertRaisesRegex(ValueError, "Attach a project"):
                git.execute(None, "create_repo", name="new", publish=True)
        call.assert_not_called()

    def test_status_from_inside_a_parent_repo_warns_and_stays_in_the_project(self):
        (self.root / "secret.txt").write_text("not in this project\n")
        nested = self.root / "sub"; nested.mkdir()
        (nested / "game.js").write_text("1\n")
        report = json.loads(agent._git_status(nested))
        self.assertIn(str(self.root.resolve()), report["warning"])
        self.assertNotIn("secret.txt", report["status"])
        own = json.loads(agent._git_status(self.root))
        self.assertNotIn("warning", own)
        self.assertIn("secret.txt", own["status"])
        self.assertIn("remotes", own)

    def test_shell_git_login_failure_points_at_the_connected_push(self):
        failed = "echo \"fatal: could not read Username for 'https://github.com': No such device\" >&2; exit 128"
        self.assertIn("action push", json.loads(agent._run_shell({"command": failed}, self.root))["hint"])
        other = json.loads(agent._run_shell({"command": "echo 'fatal: nope' >&2; exit 128"}, self.root))
        self.assertNotIn("hint", other)

    def test_push_pull_and_rejected_divergence_on_local_bare_remote(self):
        bare = self.base / "remote.git"
        subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True, check=True)
        self.run_git("remote", "add", "origin", str(bare))
        self.assertTrue(git.execute(self.root, "push")["ok"])
        other = self.base / "other"
        subprocess.run(["git", "clone", "-b", "main", str(bare), str(other)], capture_output=True, check=True)
        def other_git(*args):
            return subprocess.run(["git", "-c", "user.name=Other", "-c", "user.email=other@example.invalid", "-c", "commit.gpgsign=false", *args], cwd=other, capture_output=True, check=True)
        (other / "remote.txt").write_text("new\n"); other_git("add", "."); other_git("commit", "-m", "Remote"); other_git("push")
        self.assertTrue(git.execute(self.root, "pull_ff")["ok"])
        self.assertTrue((self.root / "remote.txt").exists())
        (other / "remote.txt").write_text("changed\n"); other_git("commit", "-am", "Remote again"); other_git("push")
        (self.root / "local.txt").write_text("local\n"); self.run_git("add", "local.txt"); self.run_git("commit", "-m", "Local")
        self.assertFalse(git.execute(self.root, "push")["ok"])
        self.assertFalse(git.execute(self.root, "pull_ff")["ok"])
        self.assertEqual((self.root / "local.txt").read_text(), "local\n")

    def test_conflicts_are_reported_and_abort_restores_source(self):
        git.execute(self.root, "branch", branch="conflict")
        (self.root / "app.py").write_text("x = 2\n"); self.run_git("commit", "-am", "Other")
        git.execute(self.root, "checkout", branch="main")
        (self.root / "app.py").write_text("x = 3\n"); self.run_git("commit", "-am", "Main")
        self.assertFalse(git.execute(self.root, "merge", branch="conflict")["ok"])
        self.assertEqual(git.execute(self.root)["conflicts"], "app.py")
        self.assertTrue(git.execute(self.root, "abort_merge")["ok"])
        self.assertEqual((self.root / "app.py").read_text(), "x = 3\n")

    def test_options_cannot_escape_or_inject_and_subprojects_cannot_modify_parent(self):
        for branch in ("--force", "main; touch bad", "../outside", "a..b"):
            with self.assertRaises(ValueError): git.execute(self.root, "branch", branch=branch)
        for value in ("../outside", ".git/config", str(self.root / "app.py"), "."):
            with self.assertRaises(ValueError): git.execute(self.root, "stage", paths=[value])
        sub = self.root / "sub"; sub.mkdir()
        with self.assertRaisesRegex(ValueError, "not at its root"): git.execute(sub, "commit", message="Bad")

    def test_github_remote_parsing_and_exact_sha_merge(self):
        self.run_git("remote", "add", "origin", "git@github.com:Example/Payments.git")
        self.assertEqual(git.repository(self.root), "Example/Payments")
        for remote in ("https://github.com/Example/Payments.git", "https://github.com/Example/Payments"):
            self.run_git("remote", "set-url", "origin", remote)
            self.assertEqual(git.repository(self.root), "Example/Payments")
        with patch.object(git, "api", return_value={"merged": True}) as api:
            with self.assertRaises(ValueError): git.execute(self.root, "merge_pull", number=12)
            git.execute(self.root, "merge_pull", number=12, sha="a" * 40)
            api.assert_called_once_with("PUT", "/repos/Example/Payments/pulls/12/merge", {"sha": "a" * 40, "merge_method": "squash"})
        self.run_git("remote", "set-url", "origin", "https://github.com.evil.invalid/Example/Payments.git")
        with self.assertRaises(ValueError): git.repository(self.root)

    def test_api_auth_headers_redirects_and_errors_do_not_expose_token(self):
        captured = []
        def handle(request):
            captured.append(request)
            return httpx.Response(403, json={"message": "token super-secret blocked"})
        real_client = httpx.Client
        with patch.object(git, "token", return_value="super-secret"), patch.object(git.httpx, "Client", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs)):
            with self.assertRaisesRegex(ValueError, r"GitHub 403: token \[redacted\] blocked"):
                git.account()
        self.assertEqual(captured[0].headers["authorization"], "Bearer super-secret")
        self.assertEqual(captured[0].url.host, "api.github.com")

    def test_askpass_only_returns_the_token_to_github_and_keeps_it_off_argv(self):
        real_run = subprocess.run
        def intercept(command, **kwargs):
            self.assertNotIn("super-secret", " ".join(command))
            helper = kwargs["env"]["GIT_ASKPASS"]
            for host, expected_code in (("github.com", 0), ("github.com.evil.invalid", 1)):
                result = real_run([helper, f"Password for 'https://x-access-token@{host}':"], env=kwargs["env"], capture_output=True, text=True)
                self.assertEqual(result.returncode, expected_code, result.stderr)
                self.assertEqual(result.stdout.strip(), "super-secret" if expected_code == 0 else "")
            return subprocess.CompletedProcess(command, 0, "token super-secret", "")
        with patch.object(git, "token", return_value="super-secret"), patch.object(git.subprocess, "run", side_effect=intercept):
            result = git._run(self.root, ["fetch", "origin"], auth=True)
            self.assertNotIn("super-secret", result["output"])


class BridgeRoutes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"; self.root.mkdir()
        self.target = self.root / "app.txt"; self.target.write_text("before\n")
        self.project = {"id": "project", "root": str(self.root), "name": "Bridge"}
        self.chats = {}
        for p in (patch.object(storage, "get_project", side_effect=lambda pid: self.project if pid == "project" else None),
                  patch.object(storage, "list_projects", return_value=[self.project]),
                  patch.object(storage, "get_chat", side_effect=lambda cid: self.chats.get(cid)),
                  patch.object(storage, "new_chat", side_effect=lambda *args: {"id": "chat", "project_id": "project", "mode": "agentic", "messages": []}),
                  patch.object(storage, "save_chat", side_effect=lambda chat: self.chats.update({chat["id"]: chat})),
                  patch.object(agent, "load_settings", return_value={"allowed_roots": [str(self.root)]}),
                  patch.object(server, "_gen_lock", asyncio.Lock()),
                  patch.object(bridge, "STORE", Path(self.temp.name) / "editor-pairings.json"),
                  patch.object(bridge, "_sessions", {}),
                  patch.object(bridge, "_loaded", False),
                  patch.object(agent, "BACKUP_DIR", Path(self.temp.name) / "backups")):
            p.start(); self.addCleanup(p.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://localhost")
        self.addAsyncCleanup(self.client.aclose)
        code = (await self.client.post("/api/ide/pair-code")).json()["code"]
        self.pair = (await self.client.post("/api/ide/connect", json={"code": code, "root": str(self.root)})).json()
        self.url = "/api/ide/" + self.pair["id"]
        self.headers = {"Authorization": "Bearer " + self.pair["token"]}
        self.chat = (await self.client.post(self.url + "/chats", json={}, headers=self.headers)).json()
        self.addCleanup(bridge.disconnect, self.pair["id"])

    async def next_edit(self):
        for _ in range(200):
            edits = (await self.client.get(self.url + "/edits", headers=self.headers)).json()["edits"]
            if edits: return edits[0]
            await asyncio.sleep(.01)
        self.fail("No editor proposal arrived")

    async def test_agent_edit_waits_for_editor_save_and_acknowledgement(self):
        work = asyncio.create_task(asyncio.to_thread(agent.run_tool, "edit_file", {"path": "app.txt", "find": "before", "replace": "after"}, self.root, chat=self.chat))
        edit = await self.next_edit()
        self.assertEqual(self.target.read_text(), "before\n")
        self.assertEqual(edit["before_sha256"], hashlib.sha256(b"before\n").hexdigest())
        response = await self.client.post(self.url + "/edits/" + edit["id"] + "/claim", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.target.write_text(edit["content"])
        response = await self.client.post(self.url + "/edits/" + edit["id"], json={"ok": True}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(await work)["changed"])

    async def test_refusal_disconnect_and_stop_never_fall_back_to_disk(self):
        for action in ("refuse", "stop", "disconnect"):
            task = asyncio.create_task(asyncio.to_thread(agent.run_tool, "write_file", {"path": "app.txt", "content": "after\n", "overwrite": True}, self.root, chat=self.chat))
            edit = await self.next_edit()
            if action == "refuse":
                await self.client.post(self.url + "/edits/" + edit["id"], json={"ok": False, "error": "Buffer changed"}, headers=self.headers)
            elif action == "stop":
                await self.client.post(self.url + "/chats/chat/stop", json={}, headers=self.headers)
            else:
                await self.client.delete(self.url, headers=self.headers)
            self.assertIn("error", json.loads(await task))
            self.assertEqual(self.target.read_text(), "before\n")

    async def test_tokens_origin_chat_scope_and_pair_reuse_are_rejected(self):
        self.assertEqual((await self.client.get(self.url + "/edits")).status_code, 401)
        self.assertEqual((await self.client.get(self.url + "/edits", headers={"Authorization": "Bearer wrong"})).status_code, 401)
        self.assertEqual((await self.client.post("/api/ide/pair-code", headers={"Origin": "https://evil.invalid"})).status_code, 403)
        code = (await self.client.post("/api/ide/pair-code")).json()["code"]
        second = (await self.client.post("/api/ide/connect", json={"code": code, "root": str(self.root)})).json()
        self.addCleanup(bridge.disconnect, second["id"])
        self.assertEqual((await self.client.post("/api/ide/connect", json={"code": code, "root": str(self.root)})).status_code, 400)
        self.assertEqual((await self.client.get(f"/api/ide/{second['id']}/chats/chat", headers={"Authorization": "Bearer " + second["token"]})).status_code, 403)

    async def test_pair_status_expiration_replacement_and_connected_session_lifetime(self):
        with patch.object(bridge, "time") as clock:
            clock.monotonic.return_value = 100
            first = (await self.client.post("/api/ide/pair-code")).json()
            self.assertEqual(first["expires_in"], 120)
            clock.monotonic.return_value = 219.1
            status = (await self.client.post("/api/ide/pair-code/status", json={"code": first["code"]})).json()
            self.assertEqual(status, {"status": "ready", "expires_in": 1})
            clock.monotonic.return_value = 220
            status = (await self.client.post("/api/ide/pair-code/status", json={"code": first["code"]})).json()
            self.assertEqual(status["status"], "expired")
            self.assertEqual((await self.client.post("/api/ide/connect", json={"code": first["code"], "root": str(self.root)})).status_code, 400)
            self.assertNotIn(first["code"], bridge._codes)
            next_code = (await self.client.post("/api/ide/pair-code")).json()["code"]
            pair = (await self.client.post("/api/ide/connect", json={"code": next_code, "root": str(self.root)})).json()
            self.addCleanup(bridge.disconnect, pair["id"])
            status = (await self.client.post("/api/ide/pair-code/status", json={"code": next_code})).json()
            self.assertEqual(status["status"], "unavailable")
            clock.monotonic.return_value = 220 + 121
            self.assertEqual((await self.client.get("/api/ide/" + pair["id"] + "/edits", headers={"Authorization": "Bearer " + pair["token"]})).status_code, 200)

    async def test_saved_pairing_can_be_forgotten_from_local_connections(self):
        malicious = await self.client.request("DELETE", "/api/ide/pairings", json={"root": str(self.root)}, headers={"Origin": "https://evil.invalid"})
        self.assertEqual(malicious.status_code, 403)
        response = await self.client.request("DELETE", "/api/ide/pairings", json={"root": str(self.root)})
        self.assertEqual(response.status_code, 200)
        bridge._sessions.clear(); bridge._loaded = False
        self.assertEqual((await self.client.get(self.url + "/edits", headers=self.headers)).status_code, 401)

    async def test_extension_download_is_an_attachment_containing_a_valid_vsix(self):
        response = await self.client.get("/api/ide/extension")
        self.assertEqual(response.status_code, 200)
        version = json.loads((Path(__file__).resolve().parents[1] / "extensions/olith-vscode/package.json").read_text())["version"]
        self.assertIn(f'attachment; filename="olith-vscode-{version}.vsix"', response.headers["content-disposition"])
        self.assertEqual(response.headers["content-type"], "application/octet-stream")
        with zipfile.ZipFile(io.BytesIO(response.content)) as artifact:
            self.assertIsNone(artifact.testzip())
            self.assertIn("extension/extension.js", artifact.namelist())

    async def test_editor_status_uses_heartbeat_without_exposing_credentials_or_expiring_pairing(self):
        start = bridge.time.monotonic()
        with patch.object(bridge, "time") as timer:
            clock = timer.monotonic
            clock.return_value = start + 1
            await self.client.get(self.url + "/edits", headers={**self.headers, "X-Olith-Extension-Version": "0.1.4"})
            seen = bridge._sessions[self.pair["id"]]["seen"]
            response = await self.client.get("/api/ide/status")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(self.pair["token"], response.text)
            self.assertNotIn(self.pair["id"], response.text)
            self.assertTrue(response.json()["sessions"][0]["connected"])
            self.assertEqual(response.json()["sessions"][0]["version"], "0.1.4")
            clock.return_value = start + 1 + bridge.ACTIVE_TTL
            response = await self.client.get("/api/ide/status")
            self.assertFalse(response.json()["sessions"][0]["connected"])
            self.assertEqual(bridge._sessions[self.pair["id"]]["seen"], seen)
            await self.client.get(self.url + "/edits", headers=self.headers)
            self.assertTrue((await self.client.get("/api/ide/status")).json()["sessions"][0]["connected"])
            clock.return_value += 90 * 24 * 3600
            self.assertFalse((await self.client.get("/api/ide/status")).json()["sessions"][0]["connected"])
            self.assertEqual((await self.client.get(self.url + "/edits", headers=self.headers)).status_code, 200)

    async def test_project_links_report_real_origin_branch_and_refresh_after_git_changes(self):
        def run(*args):
            return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True)
        run("init", "-b", "main"); run("config", "user.email", "test@example.invalid"); run("config", "user.name", "Test"); run("config", "commit.gpgsign", "false")
        run("add", "."); run("commit", "-m", "Initial"); run("remote", "add", "origin", "git@github.com:example/payments.git")
        data = (await self.client.get("/api/project-links")).json()["project"]
        self.assertEqual(data, {"git": True, "repo": "example/payments", "branch": "main"})
        response = await self.client.post("/api/projects/project/git", json={"action": "branch", "options": {"branch": "feature"}, "confirmed": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.client.get("/api/project-links")).json()["project"]["branch"], "feature")
        child = self.root / "child"; child.mkdir()
        self.assertFalse(integrations._project_link(str(child))["git"])

    async def test_github_status_and_repository_pagination(self):
        with patch.object(git, "token", return_value=None), patch.object(git, "account") as account:
            self.assertEqual((await self.client.get("/api/github/status")).json()["status"], "not_connected")
            account.assert_not_called()
        with patch.object(git, "token", return_value="test-token"), patch.object(git, "account", return_value={"login": "example", "connected": True}):
            self.assertEqual((await self.client.get("/api/github/status")).json()["login"], "example")
        with patch.object(git, "token", return_value="test-token"), patch.object(git, "account", side_effect=ValueError("GitHub rejected this token")):
            self.assertEqual((await self.client.get("/api/github/status")).json()["status"], "needs_attention")
        with patch.object(git, "api", return_value=[{"full_name": "example/second-page"}]) as api:
            self.assertEqual((await self.client.get("/api/github/repos?page=2")).json()[0]["full_name"], "example/second-page")
            self.assertIn("page=2", api.call_args.args[1])
            self.assertEqual((await self.client.get("/api/github/repos?page=0")).status_code, 422)

    async def test_extension_install_uses_only_the_bundled_package_and_reports_failures(self):
        result = subprocess.CompletedProcess([], 0, "Installed", "")
        with patch.object(integrations.shutil, "which", return_value="/usr/bin/code"), patch.object(integrations.subprocess, "run", return_value=result) as run:
            response = await self.client.post("/api/ide/install-extension", json={"path": "/outside.vsix"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("Reload Window", response.json()["note"])
            self.assertEqual(run.call_args.args[0], ["/usr/bin/code", "--install-extension", str(Path(__file__).resolve().parents[1] / "dist/olith-vscode.vsix"), "--force"])
        with patch.object(integrations.shutil, "which", return_value=None):
            self.assertEqual((await self.client.post("/api/ide/install-extension", json={})).status_code, 400)
        with patch.object(integrations.shutil, "which", return_value="/usr/bin/code"), patch.object(integrations.subprocess, "run", side_effect=subprocess.TimeoutExpired("code", 90)):
            response = await self.client.post("/api/ide/install-extension", json={})
            self.assertEqual(response.status_code, 400)
            self.assertIn("timed out", response.json()["detail"])

    async def test_editor_model_picker_is_authenticated_scoped_and_used_by_send(self):
        from fastapi.responses import JSONResponse
        registry = {
            "coder:small": {"id": "coder:small", "label": "Small coder", "tools": True},
            "coder:large": {"id": "coder:large", "label": "Large coder", "tools": True},
            "chat:only": {"id": "chat:only", "label": "Chat only", "tools": False},
            "embed:only": {"id": "embed:only", "label": "Embedding", "capabilities": ["embedding"]},
            "coder:disabled": {"id": "coder:disabled", "label": "Disabled", "tools": True},
        }
        settings = {"model_active": {"agentic": "coder:small"}, "model_roles": {"coder:disabled": [], "chat:only": ["agentic"], "embed:only": ["agentic"]}}
        with patch.object(server, "_registry", return_value=(settings, registry)):
            self.assertEqual((await self.client.get(self.url + "/models")).status_code, 401)
            data = (await self.client.get(self.url + "/models", headers=self.headers)).json()
            self.assertEqual({model["id"] for model in data["models"]}, {"coder:small", "coder:large"})
            self.assertEqual(data["active"], "coder:small")
            route = self.url + "/chats/chat/model"
            self.assertEqual((await self.client.post(route, json={"model_key": "coder:large"})).status_code, 401)
            for invalid in ("chat:only", "embed:only", "coder:disabled", "not-installed"):
                self.assertEqual((await self.client.post(route, json={"model_key": invalid}, headers=self.headers)).status_code, 400)
            self.assertEqual((await self.client.post(route, json={}, headers=self.headers)).status_code, 422)
            self.assertEqual((await self.client.post(route, content="invalid json", headers={**self.headers, "Content-Type": "application/json"})).status_code, 422)
            await server._gen_lock.acquire()
            self.assertEqual((await self.client.post(route, json={"model_key": "coder:large"}, headers=self.headers)).status_code, 409)
            server._gen_lock.release()
            response = await self.client.post(route, json={"model_key": "coder:large"}, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            saved = (await self.client.get(self.url + "/chats/chat", headers=self.headers)).json()
            self.assertEqual(saved["model_id"], "coder:large")
            self.assertEqual(settings["model_active"]["agentic"], "coder:small")
            async def send(cid, body):
                self.assertEqual(body.model_key, "coder:large")
                self.assertEqual(body.agent_model, "coder:large")
                return JSONResponse({"ok": True})
            with patch.object(server, "api_send", side_effect=send) as sender:
                response = await self.client.post(self.url + "/chats/chat/send", json={"content": "Fix it", "model_key": "coder:large", "agent_model": "coder:small"}, headers=self.headers)
                self.assertEqual(response.status_code, 200)
                for invalid in ("chat:only", "embed:only", "coder:disabled", "not-installed"):
                    for override in ({"model_key": invalid}, {"agent_model": invalid}, {"model_key": "coder:large", "agent_model": invalid}, {"model_key": invalid, "agent_model": "coder:large"}):
                        response = await self.client.post(self.url + "/chats/chat/send", json={"content": "Fix it", "mode": "chat", **override}, headers=self.headers)
                        self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(sender.call_count, 1, "Invalid overrides must not reach generation")
                response = await self.client.post(self.url + "/chats/chat/send", json={"content": "Fix it", "agent_model": "coder:large"}, headers=self.headers)
                self.assertEqual(response.status_code, 200)
            self.chats["chat"]["editor_session_id"] = "another-session"
            self.assertEqual((await self.client.post(route, json={"model_key": "coder:small"}, headers=self.headers)).status_code, 403)

    async def test_git_routes_require_confirmation_and_idle_generation(self):
        with patch.object(git, "execute", return_value={"ok": True}) as execute:
            base = "/api/projects/project/git"
            self.assertEqual((await self.client.post(base, json={"action": "push"})).status_code, 400)
            execute.assert_not_called()
            await server._gen_lock.acquire()
            self.assertEqual((await self.client.post(base, json={"action": "push", "confirmed": True})).status_code, 409)
            server._gen_lock.release()
            self.assertEqual((await self.client.post(base, json={"action": "push", "confirmed": True})).status_code, 200)
            execute.assert_called_once()
            self.assertEqual((await self.client.post(base, json={"action": "status", "options": {"mutation": False}})).status_code, 400)

    async def test_editor_scoped_send_review_and_invalid_operations(self):
        from fastapi.responses import JSONResponse
        async def send(cid, body):
            self.assertEqual(body.project_id, "project")
            self.assertEqual(body.mode, "agentic")
            return JSONResponse({"ok": True})
        with patch.object(server, "api_send", side_effect=send):
            response = await self.client.post(self.url + "/chats/chat/send", json={"content": "Fix it", "project_id": "outside", "mode": "chat"}, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
        response = await self.client.get(self.url + "/chats/chat/changes", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await self.client.post(self.url + "/chats/chat/arbitrary", json={}, headers=self.headers)).status_code, 404)

    async def test_write_and_append_new_file_also_use_the_editor(self):
        for tool, args, path in (("write_file", {"path": "new.txt", "content": "new\n"}, "new.txt"), ("append_file", {"path": "app.txt", "content": "more\n"}, "app.txt")):
            task = asyncio.create_task(asyncio.to_thread(agent.run_tool, tool, args, self.root, chat=self.chat))
            edit = await self.next_edit(); target = self.root / path
            if path == "new.txt": self.assertIsNone(edit["before_sha256"]); self.assertFalse(target.exists())
            target.write_text(edit["content"])
            await self.client.post(self.url + "/edits/" + edit["id"], json={"ok": True}, headers=self.headers)
            self.assertTrue(json.loads(await task)["ok"])

    async def test_a_save_that_vs_code_reformats_still_counts_as_written(self):
        task = asyncio.create_task(asyncio.to_thread(agent.run_tool, "edit_file", {"path": "app.txt", "find": "before", "replace": "after"}, self.root, chat=self.chat))
        edit = await self.next_edit()
        self.target.write_text(edit["content"] + "\n")
        await self.client.post(self.url + "/edits/" + edit["id"], json={"ok": True}, headers=self.headers)
        result = json.loads(await task)
        self.assertTrue(result.get("changed", result.get("ok")), result)
        self.assertNotIn("error", result)
        self.assertIn("reformatted", result["note"])

    async def test_changed_source_during_candidate_preparation_is_not_overwritten(self):
        original = agent._save_source
        def race(path, content, candidate=None):
            path.write_text("user changed during validation\n")
            return original(path, content, candidate)
        with patch.object(agent, "_save_source", side_effect=race):
            result = await asyncio.to_thread(agent.run_tool, "edit_file", {"path": "app.txt", "find": "before", "replace": "after"}, self.root, chat=self.chat)
        self.assertIn("File changed while", json.loads(result)["error"])
        self.assertEqual(self.target.read_text(), "user changed during validation\n")
        self.assertEqual(bridge.pending(self.pair["id"]), [])

    async def test_stop_waits_for_a_claimed_editor_save(self):
        task = asyncio.create_task(asyncio.to_thread(agent.run_tool, "edit_file", {"path": "app.txt", "find": "before", "replace": "after"}, self.root, chat=self.chat))
        edit = await self.next_edit()
        await self.client.post(self.url + "/edits/" + edit["id"] + "/claim", json={}, headers=self.headers)
        await self.client.post(self.url + "/chats/chat/stop", json={}, headers=self.headers)
        self.assertFalse(task.done())
        self.target.write_text(edit["content"])
        await self.client.post(self.url + "/edits/" + edit["id"], json={"ok": True}, headers=self.headers)
        self.assertTrue(json.loads(await task)["changed"])


class GitApprovalTests(unittest.IsolatedAsyncioTestCase):
    def test_the_prompt_says_when_a_new_repo_is_public(self):
        why = lambda options: server._git_approval_why({"action": "create_repo", "options": options})
        self.assertEqual(why({"name": "x"}), "Creates a private GitHub repository")
        self.assertEqual(why({"name": "x", "private": False, "publish": True}), "Creates a PUBLIC GitHub repository and pushes this project")
        # The same reading execute gives it: a stray string stays private.
        self.assertIn("private", why({"private": "false"}))
        self.assertEqual(server._git_approval_why({"action": "push", "options": "junk"}), "Repository changes require your approval")
        self.assertEqual(server._git_approval_why({"action": "create_repo", "options": "junk"}), "Creates a private GitHub repository")

    def test_a_new_repo_is_private_unless_explicitly_public(self):
        with patch.object(git, "api", return_value={}) as api:
            git.execute(None, "create_repo", name="x")
            self.assertIs(api.call_args.args[2]["private"], True)
            git.execute(None, "create_repo", name="x", private="false")
            self.assertIs(api.call_args.args[2]["private"], True)

    async def test_agent_mutation_requires_approval_even_with_shell_auto_approve(self):
        import repo_index
        import project_commands
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = {"allowed_roots": [], "shell_approval_mode": "never_ask", "repo_map_enabled": False, "language_servers": False, "task_change_review": False, "auto_verify_project": False}
            for approved in (True, False):
                chat = {"id": "git-approval", "mode": "agentic", "project_id": "p", "messages": [{"role": "user", "content": "Push this branch"}]}
                samples = 0
                async def stream(*args, **kwargs):
                    nonlocal samples
                    samples += 1
                    if samples == 1:
                        yield {"message": {"tool_calls": [{"function": {"name": "git_status", "arguments": {"action": "push", "options": {"branch": "main"}}}}]}, "done": True}
                    else:
                        yield {"message": {"content": "Finished."}, "done": True}
                with patch.object(server.st, "get_project", return_value={"root": str(root), "name": "Git"}), patch.object(server.st, "load_settings", return_value=settings), patch.object(agent, "load_settings", return_value=settings), \
                     patch.object(server.st, "save_chat"), patch.object(server.st, "memory_as_prompt", return_value=""), patch.object(server.st, "history_index", return_value=""), patch.object(server.st, "canon_as_prompt", return_value=""), \
                     patch.object(repo_index, "INDEX_DIR", root / "indexes"), patch.object(project_commands, "COMMAND_DIR", root / "commands"), patch.object(server.oc, "stream_chat", side_effect=stream), patch.object(git, "execute", return_value={"ok": True}) as execute:
                    prompted = False
                    async for event in server._agent_loop(chat, {"id": "test", "tools": True, "keep_alive": "0"}, "test"):
                        if event["type"] == "shell_approval":
                            prompted = True
                            execute.assert_not_called()
                            # The bar must not offer a remember box this prompt would ignore.
                            self.assertIs(event["rememberable"], False)
                            self.assertEqual(event["binary"], "")
                            await server.answer_question(chat["id"], server.AnswerIn(answers={"shell_approval": "Run command"} if approved else {}, cancelled=not approved))
                    self.assertTrue(prompted)
                    self.assertEqual(execute.call_count, int(approved))


class PackagingTests(unittest.TestCase):
    def test_installable_vsix_has_manifest_license_and_runtime_files(self):
        with tempfile.TemporaryDirectory() as folder:
            artifact = package(Path(folder) / "olith.vsix")
            with zipfile.ZipFile(artifact) as archive:
                self.assertIsNone(archive.testzip())
                self.assertTrue({"extension.vsixmanifest", "[Content_Types].xml", "extension/package.json", "extension/extension.js", "extension/client.js", "extension/LICENSE"}.issubset(archive.namelist()))
                metadata = json.loads(archive.read("extension/package.json"))
                self.assertFalse(metadata.get("dependencies"))
                self.assertFalse(metadata["capabilities"]["untrustedWorkspaces"]["supported"])
                for name in ("webview.js", "media/chat.css", "media/chat.js", "media/markdown.js", "media/olith.svg"):
                    self.assertIn("extension/" + name, archive.namelist())
                self.assertIn(b'Extension="css" ContentType="text/css"', archive.read("[Content_Types].xml"))
                self.assertEqual(archive.read("extension/media/olith.svg"), (Path(__file__).resolve().parents[1] / "static/assets/olith-mark.svg").read_bytes())
                self.assertEqual(metadata["icon"], "media/olith.png")
                container = metadata["contributes"]["viewsContainers"]["activitybar"][0]
                self.assertEqual(container["icon"], "media/olith-activity.svg")
                self.assertNotEqual(container["id"], "olith")
                self.assertEqual(metadata["contributes"]["views"][container["id"]][0]["id"], "olith.chat")
                self.assertEqual(archive.read("extension/" + container["icon"]), (Path(__file__).resolve().parents[1] / "static/assets/olith-mark.svg").read_bytes())
                self.assertEqual(archive.read("extension/" + metadata["icon"]), (Path(__file__).resolve().parents[1] / "static/assets/olith.png").read_bytes())
                self.assertIn(b'Microsoft.VisualStudio.Services.Icons.Default" Path="extension/media/olith.png"', archive.read("extension.vsixmanifest"))
                self.assertIn(b'Extension="png" ContentType="image/png"', archive.read("[Content_Types].xml"))
