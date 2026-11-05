# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Exercise the UI API and agent against a real temporary project/index."""
import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import agent
import repo_index
import project_commands
import task_changes
from tests.test_repo_index import FakeEmbedder

try:
    import httpx
    import server
except ModuleNotFoundError:
    server = None


@unittest.skipIf(server is None, "Server dependencies unavailable")
class IndexRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "project"
        self.root.mkdir()
        (self.root / "auth.py").write_text("def verify_token(token):\n    return bool(token)\n")
        self.project = {"id": "index-test", "name": "Index test", "root": str(self.root)}
        self.settings = {"semantic_search": False, "embedding_model": "fake", "allowed_roots": []}
        for patcher in (
            patch.object(repo_index, "INDEX_DIR", base / "cache"),
            patch.object(project_commands, "COMMAND_DIR", base / "commands"),
            patch.object(task_changes, "CHANGE_DIR", base / "changes"),
            patch.object(server.st, "get_project", side_effect=lambda pid: self.project if pid == "index-test" else None),
            patch.object(server.st, "load_settings", side_effect=lambda: self.settings),
            patch.object(agent, "load_settings", side_effect=lambda: self.settings),
            patch.object(server, "_gen_lock", asyncio.Lock()),
            patch.object(server, "_index_tasks", {}),
            patch.object(server, "_index_states", {}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://localhost")
        self.addAsyncCleanup(self.client.aclose)

    async def finish_job(self):
        task = server._index_tasks.get("index-test")
        if task:
            await task

    async def test_build_search_status_and_agent_share_one_index(self):
        response = await self.client.post("/api/projects/index-test/index", json={})
        self.assertEqual(response.status_code, 200)
        await self.finish_job()
        status = (await self.client.get("/api/projects/index-test/index")).json()
        self.assertEqual(status["files"], 1)
        self.assertEqual(status["job"]["phase"], "ready")
        response = await self.client.post("/api/projects/index-test/index/search", json={"query": "verify_token", "mode": "symbols"})
        self.assertEqual(response.json()["hits"][0]["file"], "auth.py")
        result = json.loads(agent.run_tool("search_files", {"pattern": "verify_token", "mode": "hybrid"}, self.root))
        self.assertEqual(result["hits"][0]["line"], 1)
        self.assertNotIn("error", result)
        regex = json.loads(agent.run_tool("search_files", {"pattern": "def verify_token"}, self.root))
        self.assertTrue(regex["hits"])
        self.assertNotIn("mode", regex)

    async def test_unknown_project_invalid_modes_and_cross_origin_are_rejected(self):
        self.assertEqual((await self.client.get("/api/projects/missing/index")).status_code, 404)
        self.assertEqual((await self.client.post("/api/projects/index-test/index/search", json={"query": "token", "mode": "invented"})).status_code, 400)
        self.assertEqual((await self.client.post("/api/projects/index-test/index", json={"semantic": True})).status_code, 400)
        self.assertEqual((await self.client.post("/api/projects/index-test/index", json={}, headers={"Origin": "https://example.com"})).status_code, 403)

    async def test_manual_commands_and_selected_undo_round_trip(self):
        response = await self.client.put("/api/projects/index-test/commands", json={"commands": [{"kind": "test", "command": "python -m unittest", "cwd": "."}]})
        self.assertEqual(response.status_code, 200)
        response = await self.client.get("/api/projects/index-test/commands")
        self.assertEqual(response.json()["commands"][0]["source"], "manual")
        target = self.root / "auth.py"
        before = target.read_text()
        review = task_changes.ChangeSet(self.root).start()
        target.write_text(before.replace("bool(token)", "token is not None"))
        changes = review.finish(True)
        chat = {"id": "c", "project_id": "index-test", "change_set_id": review.ident}
        selected = {"change_set_id": review.ident, "selections": [{"file": "auth.py", "hunks": [changes["files"][0]["hunks"][0]["id"]]}]}
        with patch.object(server.st, "get_chat", return_value=chat), patch.object(server.st, "save_chat"):
            response = await self.client.get("/api/chats/c/changes")
            self.assertEqual(len(response.json()["files"]), 1)
            await server._gen_lock.acquire()
            try:
                self.assertEqual((await self.client.post("/api/chats/c/changes/undo", json=selected)).status_code, 409)
            finally:
                server._gen_lock.release()
            response = await self.client.post("/api/chats/c/changes/undo", json=selected)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["files"], [])
        self.assertEqual(target.read_text(), before)

    async def test_embeddings_wait_for_generation_and_can_stop_while_waiting(self):
        self.settings["semantic_search"] = True
        await server._gen_lock.acquire()
        with patch.object(repo_index, "OllamaEmbedder", return_value=FakeEmbedder()) as embedder:
            await self.client.post("/api/projects/index-test/index", json={"semantic": True})
            # The background task should reach the model lock without blocking UI.
            for _ in range(100):
                if server._index_states["index-test"]["phase"] == "waiting":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(server._index_states["index-test"]["phase"], "waiting")
            self.assertEqual((await self.client.post("/api/projects/index-test/index/stop", json={})).status_code, 200)
            server._gen_lock.release()
            await self.finish_job()
            self.assertEqual(server._index_states["index-test"]["phase"], "stopped")
            self.assertEqual(embedder.return_value.calls, 0)

    async def test_semantic_build_and_automatic_incremental_refresh(self):
        self.settings["semantic_search"] = True
        with patch.object(repo_index, "OllamaEmbedder", return_value=FakeEmbedder()):
            await self.client.post("/api/projects/index-test/index", json={"semantic": True})
            await self.finish_job()
            status = (await self.client.get("/api/projects/index-test/index")).json()
            self.assertEqual(status["vectors"], 1)
            (self.root / "new.py").write_text("def invoice(): pass")
            response = await self.client.post("/api/projects/index-test/index/search", json={"query": "invoice"})
            self.assertEqual(response.status_code, 200)
            await self.finish_job()
            status = (await self.client.get("/api/projects/index-test/index")).json()
            self.assertEqual(status["vectors"], 2)

    async def test_prompt_map_is_explicit_and_stable_between_steps(self):
        index = repo_index.ProjectIndex(self.root)
        context = index.repo_map("verify token", 400)
        chat = {"id": "frozen-map", "mode": "agentic", "project_id": "index-test", "messages": [{"role": "user", "content": "verify tokens"}]}
        with patch.object(server.st, "memory_as_prompt", return_value=""), \
             patch.object(server.st, "history_index", return_value=""), \
             patch.object(server.st, "canon_as_prompt", return_value=""), \
             patch.object(server.reg, "installed", return_value={}):
            before = server._build_messages(chat, "agent", repo_context=context)[0]["content"]
            (self.root / "auth.py").write_text("def changed(): pass")
            chat["messages"].append({"role": "assistant", "content": "Inspecting"})
            after = server._build_messages(chat, "agent", repo_context=context)[0]["content"]
        self.assertEqual(before, after)
        self.assertIn("verify_token", before)

    async def test_cancellation_waits_for_the_embedding_worker(self):
        started, release = threading.Event(), threading.Event()
        def worker():
            started.set()
            release.wait(3)
        task = asyncio.create_task(server._index_work(worker))
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.01)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()
