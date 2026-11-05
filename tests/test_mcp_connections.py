# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real MCP transports, OAuth/PKCE, tool dispatch, and local UI contracts."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import agent
import connections
import mcp_connections as mc
import server

FIXTURE = Path(__file__).with_name("mcp_fixture.py")


def payload(result):
    if result.get("error"):
        raise AssertionError(result)
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


class ConnectionCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.vault = {}
        def clear(cid):
            for key in list(self.vault):
                if key[0] == cid:
                    del self.vault[key]
        for p in (patch.object(mc, "STORE", self.base / "connections.json"),
                  patch.object(mc, "_get_secret", side_effect=lambda cid, part: self.vault.get((cid, part))),
                  patch.object(mc, "_set_secret", side_effect=lambda cid, part, value: self.vault.update({(cid, part): value})),
                  patch.object(mc, "_clear_secrets", side_effect=clear),
                  patch.object(connections, "keyring_backend", return_value=(True, "fixture"))):
            p.start(); self.addCleanup(p.stop)

    def local(self, **extra):
        return mc.save({"id": "fixture", "label": "Fixture", "transport": "stdio", "command": sys.executable,
                        "args": [str(FIXTURE)], "auth": "none", **extra})


class RegistryTests(ConnectionCase):
    def test_validates_ids_urls_auth_and_environment(self):
        for change in ({"id": "../x"}, {"url": "http://remote.example/mcp"}, {"url": "https://u:password@host.example/mcp"},
                       {"url": "https://host.example/mcp?access_token=secret"}, {"auth": "bad"}, {"transport": "pipe"},
                       {"env": {"BAD NAME": "secret"}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                mc.save({"id": "test", "url": "https://example.invalid/mcp", **change})
        self.assertEqual(mc.listing()["connections"], [])

    def test_credentials_are_kept_out_of_registry_listing_and_prompt(self):
        mc.save({"id": "test", "url": "https://example.invalid/mcp", "auth": "bearer", "token": "SENSITIVE-TOKEN",
                 "client_secret": "SENSITIVE-CLIENT", "env": {"KEY": "SENSITIVE-ENV"}})
        self.assertIn(("test", "token"), self.vault)
        for output in (mc.STORE.read_text(), json.dumps(mc.listing()), mc.prompt_block()):
            self.assertNotIn("SENSITIVE", output)

    def test_replacing_endpoint_erases_credentials_and_tools(self):
        mc.save({"id": "test", "url": "https://one.invalid/mcp", "auth": "bearer", "token": "TOKEN"})
        mc._update("test", status="connected", tools=[{"name": "read"}])
        entry = mc.save({"id": "test", "url": "https://two.invalid/mcp", "auth": "bearer"})
        self.assertFalse(entry["has_token"])
        self.assertEqual(self.vault, {})
        self.assertEqual(entry["tool_count"], 0)
        self.assertEqual(mc.active(), [])

    def test_disabled_or_untested_connections_offer_no_agent_tools(self):
        self.local()
        self.assertFalse(any(t["function"]["name"].startswith("connection_") for t in agent.tool_defs_for({})))
        mc._update("fixture", status="connected", tools=[])
        offered = {t["function"]["name"] for t in agent.tool_defs_for({})}
        self.assertIn("connection_tools", offered)
        self.assertIn("connection_call", offered)
        mc.set_enabled("fixture", False)
        self.assertNotIn("connection_call", {t["function"]["name"] for t in agent.tool_defs_for({})})
        self.assertEqual(mc.prompt_block(), "")

    def test_subtasks_cannot_use_direct_connections(self):
        self.local()
        mc._update("fixture", status="connected", tools=[])
        names = {t["function"]["name"] for t in agent.tool_defs_for({"subagent": {"tools": ["connection_call", "connection_tools"]}})}
        self.assertNotIn("connection_call", names)
        self.assertNotIn("connection_tools", names)

    def test_railway_and_other_presets_are_available_without_being_connected(self):
        items = mc.listing()
        self.assertGreaterEqual(len(items["presets"]), 10)
        railway = next(p for p in items["presets"] if p["id"] == "railway")
        self.assertEqual(railway["url"], "https://mcp.railway.com")
        self.assertEqual(railway["auth"], "oauth")
        self.assertEqual(items["connections"], [])

    def test_vercel_missing_client_fails_immediately_without_a_network_attempt(self):
        mc.save({"id": "vercel", "url": "https://mcp.vercel.com", "auth": "oauth"})
        with patch.object(mc, "_session", side_effect=AssertionError("must not contact the provider yet")):
            result = asyncio.run(mc.start_connect("vercel"))
        self.assertFalse(result["busy"])
        self.assertEqual(result["status"], "needs attention")
        self.assertIn("requires an OAuth client ID", result["note"])
        self.assertEqual(mc.active(), [])

    def test_an_https_client_metadata_identity_is_sent_as_the_oauth_client_id(self):
        mc.save({"id": "vercel", "url": "https://mcp.vercel.com", "auth": "oauth", "client_id": "https://app.example/olith-client.json"})
        info = asyncio.run(mc._OAuthStorage(mc._load()["vercel"]).get_client_info())
        self.assertEqual(info.client_id, "https://app.example/olith-client.json")
        self.assertEqual(str(info.redirect_uris[0]), mc.callback_url("vercel"))
        self.assertEqual(info.token_endpoint_auth_method, "none")


class RealStdioTests(ConnectionCase):
    def connect(self, **options):
        self.local(**options)
        result = asyncio.run(mc.test_connection("fixture"))
        self.assertEqual(result["status"], "connected", result)
        return result

    def test_discovery_and_read_call_use_a_real_local_server(self):
        result = self.connect()
        self.assertEqual(result["tool_count"], 2)
        overview = mc.discover({})
        self.assertEqual(overview["servers"][0]["id"], "fixture")
        tools = mc.discover({"query": "lookup"})["tools"]
        self.assertEqual(tools[0]["inputSchema"]["required"], ["query"])
        result = json.loads(agent.run_tool("connection_call", {"server": "fixture", "tool": "lookup", "arguments": {"query": "hello"}}, None))
        self.assertFalse(result["isError"], result)
        self.assertEqual(payload(result)["query"], "hello")

    def test_mutating_tools_require_approval_and_denials_never_execute(self):
        marker = self.base / "note.txt"
        self.connect(env={"FIXTURE_MARKER": str(marker)})
        args = {"server": "fixture", "tool": "write_note", "arguments": {"text": "approved"}}
        self.assertTrue(mc.requires_approval(args))
        self.assertTrue(mc.call(args)["needs_approval"])
        self.assertFalse(marker.exists())
        result = mc.call(args, approved=True)
        self.assertFalse(result["isError"], result)
        self.assertEqual(marker.read_text(), "approved")

    def test_inputs_disabled_connections_and_unknown_tools_are_rejected(self):
        self.connect()
        for args in ({"server": "fixture", "tool": "lookup", "arguments": {}},
                     {"server": "fixture", "tool": "unknown", "arguments": {}},
                     {"server": "unknown", "tool": "lookup", "arguments": {"query": "x"}}):
            self.assertIn("error", mc.preflight(args))
        mc.set_enabled("fixture", False)
        self.assertIn("error", mc.call({"server": "fixture", "tool": "lookup", "arguments": {"query": "x"}}, approved=True))

    def test_changed_live_tool_annotations_block_execution(self):
        marker = self.base / "note.txt"
        self.connect(env={"FIXTURE_MARKER": str(marker), "FIXTURE_WRITE_READONLY": "1"})
        self.vault[("fixture", "env")] = json.dumps({"FIXTURE_MARKER": str(marker), "FIXTURE_WRITE_READONLY": "0"})
        args = {"server": "fixture", "tool": "write_note", "arguments": {"text": "must not write"}}
        result = mc.call(args)
        self.assertIn("definitions changed", result["error"])
        self.assertFalse(marker.exists())
        self.assertEqual(mc.active(), [])

    def test_secret_values_are_redacted_from_tool_results(self):
        secret = 'SECRET-with-quote"and-雪'
        self.connect(env={"FIXTURE_SECRET": secret})
        result = mc.call({"server": "fixture", "tool": "lookup", "arguments": {"query": "x"}})
        self.assertFalse(result.get("error"), result)
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertIn("[redacted]", json.dumps(result))

    def test_missing_command_is_an_actionable_failure(self):
        self.local(command=str(self.base / "not-installed"))
        result = asyncio.run(mc.test_connection("fixture"))
        self.assertEqual(result["status"], "needs attention")
        self.assertIn("not found", result["note"])

    def test_provider_tool_errors_are_reported_as_failures_to_the_agent(self):
        self.connect()
        result = mc.call({"server": "fixture", "tool": "lookup", "arguments": {"query": "error"}})
        self.assertTrue(result["isError"])
        self.assertFalse(server._tool_succeeded("connection_call", json.dumps(result)))


class HTTPProvider:
    def __init__(self, mode):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.process = subprocess.Popen([sys.executable, str(FIXTURE), mode, str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise AssertionError("HTTP fixture exited during startup")
                try:
                    httpx.get(self.url, timeout=.3)
                    return
                except httpx.TransportError:
                    time.sleep(.05)
            raise AssertionError("HTTP fixture did not start")
        except BaseException:
            self.close()
            raise
    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill(); self.process.wait(timeout=5)


class RealHTTPTests(ConnectionCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = HTTPProvider("bearer")
    @classmethod
    def tearDownClass(cls): cls.provider.close()

    def test_http_bearer_connect_discover_call_and_forget(self):
        mc.save({"id": "http", "url": self.provider.url + "/mcp", "auth": "bearer", "token": "fixture-bearer"})
        result = asyncio.run(mc.test_connection("http"))
        self.assertEqual(result["status"], "connected", result)
        result = mc.call({"server": "http", "tool": "lookup", "arguments": {"query": "http"}})
        self.assertEqual(payload(result)["query"], "http")
        asyncio.run(mc.forget("http"))
        self.assertEqual(self.vault, {})
        self.assertEqual(mc.listing()["connections"], [])

    def test_wrong_credentials_do_not_promise_working_tools(self):
        mc.save({"id": "http", "url": self.provider.url + "/mcp", "auth": "bearer", "token": "wrong-secret-token"})
        result = asyncio.run(mc.test_connection("http"))
        self.assertEqual(result["status"], "needs attention", result)
        self.assertIn("401", result["note"])
        self.assertNotIn("wrong-secret-token", json.dumps(result))
        self.assertEqual(mc.active(), [])


class OAuthTests(ConnectionCase):
    @classmethod
    def setUpClass(cls): cls.provider = HTTPProvider("oauth")
    @classmethod
    def tearDownClass(cls): cls.provider.close()

    def test_real_oauth_pkce_callback_persistence_refresh_and_replay_protection(self):
        async def scenario():
            mc.save({"id": "oauth", "url": self.provider.url + "/mcp", "auth": "oauth"})
            await mc.start_connect("oauth")
            for _ in range(150):
                status = mc.auth_status("oauth")
                if status["authorization_url"] or not status["busy"]:
                    break
                await asyncio.sleep(.03)
            self.assertTrue(status["authorization_url"], status)
            with self.assertRaises(ValueError):
                mc.finish_callback("oauth", "bad-code", "wrong-state")
            async with httpx.AsyncClient(follow_redirects=False) as client:
                response = await client.get(status["authorization_url"])
            params = parse_qs(urlsplit(response.headers["location"]).query)
            mc.finish_callback("oauth", params["code"][0], params["state"][0])
            with self.assertRaises(ValueError):
                mc.finish_callback("oauth", params["code"][0], params["state"][0])
            await mc._jobs["oauth"]["task"]
            self.assertEqual(mc.auth_status("oauth")["status"], "connected", mc.auth_status("oauth"))
            raw = mc.STORE.read_text()
            self.assertNotIn("fixture-token", raw)
            self.assertNotIn("fixture-refresh", raw)
            first_token = self.vault[("oauth", "oauth_tokens")]
            result = await mc.call_async({"server": "oauth", "tool": "lookup", "arguments": {"query": "oauth"}})
            self.assertEqual(payload(result)["query"], "oauth", result)
            stored = json.loads(self.vault[("oauth", "oauth_tokens")])
            stored["expires_at"] = time.time() - 1
            self.vault[("oauth", "oauth_tokens")] = json.dumps(stored)
            async with httpx.AsyncClient() as client:
                await client.post(self.provider.url + "/expire")
            result = await mc.call_async({"server": "oauth", "tool": "lookup", "arguments": {"query": "refreshed"}})
            self.assertEqual(payload(result)["query"], "refreshed", result)
            self.assertNotEqual(self.vault[("oauth", "oauth_tokens")], first_token)
            self.assertEqual(json.loads(self.vault[("oauth", "oauth_tokens")])["tokens"]["refresh_token"], "fixture-refresh")
            await mc.forget("oauth")
            self.assertEqual(self.vault, {})
        asyncio.run(scenario())


class APITests(ConnectionCase):
    def test_api_configure_connect_pause_and_cross_origin_protection(self):
        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://localhost") as client:
                body = {"id": "fixture", "transport": "stdio", "command": sys.executable, "args": [str(FIXTURE)], "auth": "none"}
                denied = await client.post("/api/tool-connections", json=body, headers={"origin": "https://evil.invalid"})
                self.assertEqual(denied.status_code, 403)
                self.assertEqual(mc.listing()["connections"], [])
                saved = await client.post("/api/tool-connections", json=body)
                self.assertEqual(saved.status_code, 200, saved.text)
                self.assertEqual((await client.post("/api/tool-connections/fixture/connect")).status_code, 200)
                await mc._jobs["fixture"]["task"]
                status = (await client.get("/api/tool-connections/fixture/status")).json()
                self.assertEqual(status["status"], "connected", status)
                self.assertFalse(status["busy"])
                self.assertEqual((await client.patch("/api/tool-connections/fixture", json={"enabled": False})).status_code, 200)
                self.assertEqual(mc.active(), [])
                bad = await client.get("/api/tool-connections/fixture/callback?code=secret&state=bad")
                self.assertEqual(bad.status_code, 400)
                self.assertNotIn("secret", bad.text)
                self.assertEqual((await client.delete("/api/tool-connections/fixture")).status_code, 200)
        asyncio.run(scenario())


class ApprovalLoopTests(ConnectionCase):
    def test_service_writes_are_approved_or_denied_in_the_real_agent_loop(self):
        for approve in (False, True):
            with self.subTest(approve=approve):
                marker = self.base / ("approved.txt" if approve else "denied.txt")
                self.local(env={"FIXTURE_MARKER": str(marker)})
                self.assertEqual(asyncio.run(mc.test_connection("fixture"))["status"], "connected")
                chat = {"id": "mcp-approval-test", "mode": "agentic", "messages": [{"role": "user", "content": "Save my note using Fixture"}]}
                count = 0
                async def stream(*args, **kwargs):
                    nonlocal count
                    count += 1
                    message = {"tool_calls": [{"function": {"name": "connection_call", "arguments": {"server": "fixture", "tool": "write_note", "arguments": {"text": "saved"}, "approved": True}}}]} if count == 1 else {"content": "Finished the requested action."}
                    yield {"message": message, "done": True}
                async def scenario():
                    approvals = []
                    async for event in server._agent_loop(chat, {"id": "fixture", "tools": True, "keep_alive": "0"}, "fixture"):
                        if event["type"] == "shell_approval":
                            approvals.append(event)
                            server._pending_questions[chat["id"]]["future"].set_result({"answers": {"shell_approval": "Run command" if approve else "Deny"}})
                    self.assertEqual(len(approvals), 1)
                    self.assertFalse(approvals[0]["rememberable"])
                    self.assertIn("write_note", approvals[0]["command"])
                settings = {"shell_approval_mode": "never_ask", "repo_map_enabled": False, "agent_tool_budget": 4}
                with (patch.object(server.oc, "stream_chat", side_effect=stream),
                      patch.object(server.st, "load_settings", return_value=settings),
                      patch.object(server.st, "save_chat", side_effect=lambda value: value),
                      patch.object(server.st, "memory_as_prompt", return_value=""),
                      patch.object(server.st, "history_index", return_value=""),
                      patch.object(server.st, "canon_as_prompt", return_value="")):
                    asyncio.run(scenario())
                self.assertEqual(marker.exists(), approve)
                if approve:
                    self.assertEqual(marker.read_text(), "saved")

    def test_connection_replacement_invalidates_an_approval_ticket(self):
        self.local()
        self.assertEqual(asyncio.run(mc.test_connection("fixture"))["status"], "connected")
        args = {"server": "fixture", "tool": "write_note", "arguments": {"text": "x"}}
        ticket = mc.approval_ticket(args)
        self.local()
        self.assertEqual(asyncio.run(mc.test_connection("fixture"))["status"], "connected")
        result = mc.call(args, approved=True, ticket=ticket)
        self.assertIn("changed while awaiting approval", result["error"])

    def test_remote_schema_references_are_never_fetched(self):
        self.local()
        mc._update("fixture", status="connected", tools=[{"name": "bad", "inputSchema": {"$ref": "https://other.invalid/schema"}}])
        result = mc.preflight({"server": "fixture", "tool": "bad", "arguments": {}})
        self.assertIn("local references", result["error"])

    def test_old_oauth_workers_cannot_store_credentials_after_settings_change(self):
        self.local()
        old = mc._load()["fixture"]
        self.local(command="another-server")
        with self.assertRaises(mc.SignInRequired):
            mc._store_oauth(old, "oauth_tokens", "must-not-save")
        self.assertNotIn(("fixture", "oauth_tokens"), self.vault)


class OAuthCancellationTests(ConnectionCase):
    @classmethod
    def setUpClass(cls): cls.provider = HTTPProvider("oauth")
    @classmethod
    def tearDownClass(cls): cls.provider.close()

    def test_declined_sign_in_and_cancellation_recover_cleanly(self):
        async def wait_for_auth():
            for _ in range(150):
                status = mc.auth_status("oauth")
                if status["authorization_url"] or not status["busy"]:
                    return status
                await asyncio.sleep(.03)
            self.fail("Authorization URL never appeared")
        async def scenario():
            mc.save({"id": "oauth", "url": self.provider.url + "/mcp", "auth": "oauth"})
            await mc.start_connect("oauth")
            status = await wait_for_auth()
            state = parse_qs(urlsplit(status["authorization_url"]).query)["state"][0]
            mc.finish_callback("oauth", None, state, "access_denied")
            await mc._jobs["oauth"]["task"]
            self.assertEqual(mc.auth_status("oauth")["status"], "needs attention")
            self.assertIn("declined", mc.auth_status("oauth")["note"])
            await mc.start_connect("oauth")
            self.assertTrue((await wait_for_auth())["authorization_url"])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://localhost") as client:
                response = await client.post("/api/tool-connections/oauth/cancel")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(mc.auth_status("oauth")["status"], "not tested")
            self.assertFalse(mc.auth_status("oauth")["busy"])
            self.assertIsNone(mc.auth_status("oauth")["authorization_url"])
            self.assertEqual(mc.active(), [])
            await mc.forget("oauth")
        asyncio.run(scenario())

    def test_callback_query_is_removed_before_access_logging(self):
        from starlette.requests import Request
        scope = {"type": "http", "method": "GET", "path": "/callback", "headers": [], "query_string": b"code=sensitive-code&state=invalid"}
        import mcp_routes
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            asyncio.run(mcp_routes.callback("missing", Request(scope)))
        self.assertEqual(scope["query_string"], b"")


class ProjectEvidenceTests(ConnectionCase):
    def test_local_mcp_file_changes_invalidate_earlier_test_results(self):
        import project_commands
        import shlex
        root = self.base / "project"
        root.mkdir()
        source = root / "source.txt"
        source.write_text("original")
        (root / "test_existing.py").write_text("import unittest\nclass TestExisting(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n")
        self.local(env={"FIXTURE_MARKER": str(source)})
        self.assertEqual(asyncio.run(mc.test_connection("fixture"))["status"], "connected")
        chat = {"id": "mcp-project-evidence", "mode": "agentic", "project_id": "p", "messages": [{"role": "user", "content": "Run tests, then change the note"}]}
        rounds = 0
        async def stream(*args, **kwargs):
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                message = {"tool_calls": [{"function": {"name": "run_shell", "arguments": {"command": shlex.quote(sys.executable) + " -m unittest"}}}]}
            elif rounds == 2:
                message = {"tool_calls": [{"function": {"name": "connection_call", "arguments": {"server": "fixture", "tool": "write_note", "arguments": {"text": "changed"}}}}]}
            else:
                message = {"content": "Changed the note."}
            yield {"message": message, "done": True}
        async def scenario():
            async for event in server._agent_loop(chat, {"id": "fixture", "tools": True, "keep_alive": "0"}, "fixture"):
                if event["type"] == "shell_approval":
                    server._pending_questions[chat["id"]]["future"].set_result({"answers": {"shell_approval": "Run command"}})
        settings = {"allowed_roots": [str(root)], "shell_approval_mode": "never_ask", "repo_map_enabled": False, "task_change_review": False, "auto_verify_project": False, "agent_tool_budget": 5}
        with (patch.object(server.oc, "stream_chat", side_effect=stream),
              patch.object(server.st, "get_project", return_value={"root": str(root), "name": "Fixture"}),
              patch.object(server.st, "load_settings", return_value=settings),
              patch.object(agent, "load_settings", return_value=settings),
              patch.object(project_commands, "COMMAND_DIR", self.base / "commands"),
              patch.object(server.st, "save_chat", side_effect=lambda value: value),
              patch.object(server.st, "memory_as_prompt", return_value=""),
              patch.object(server.st, "history_index", return_value=""),
              patch.object(server.st, "canon_as_prompt", return_value="")):
            asyncio.run(scenario())
        self.assertEqual(source.read_text(), "changed")
        self.assertIn("Nothing ran after the last change", chat["messages"][-1]["content"])
        self.assertNotIn("Checked with tests that already existed", chat["messages"][-1]["content"])
