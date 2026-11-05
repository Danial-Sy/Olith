# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Test browser sessions, API-key storage and the accounts shown to the model.

Users sign in through Olith's browser. Keys stay in the OS keyring and never
appear in model prompts.
"""
import json
import os
import re
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent
import connections
import server


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self.secrets = {}
        patcher = patch.object(connections, "_load", lambda: json.loads(json.dumps(self.store)))
        patcher.start(); self.addCleanup(patcher.stop)
        saver = patch.object(connections, "_save", lambda d: self.store.update(d))
        saver.start(); self.addCleanup(saver.stop)
        for name, fn in (
            ("secret", lambda cid: self.secrets.get(cid)),
            ("set_secret", lambda cid, v: self.secrets.__setitem__(cid, v)),
            ("_forget_secret", lambda cid: self.secrets.pop(cid, None)),
        ):
            p = patch.object(connections, name, fn)
            p.start(); self.addCleanup(p.stop)
        self.store["connections"] = {}

    def test_a_known_service_brings_its_own_details(self):
        entry = connections.add("google")
        self.assertEqual(entry["label"], "Google")
        self.assertEqual(entry["kind"], "session")
        self.assertEqual(entry["status"], "not connected")

    def test_a_session_connection_stores_no_secret(self):
        # There is nothing to store: the user signs in themselves and the
        # cookie lives in the browser profile, which is the only arrangement
        # Google and the rest actually accept.
        connections.add("google")
        connections._mark("google", status="connected")
        self.assertEqual(self.secrets, {})

    def test_a_key_goes_to_the_keyring_and_not_to_the_listing(self):
        with patch.object(connections, "keyring_backend", return_value=(True, "test")):
            connections.store_secret("notion", "super-secret-token")
        self.assertEqual(self.secrets["notion"], "super-secret-token")
        self.assertNotIn("super-secret-token", json.dumps(connections.listing()))

    def test_the_listing_reports_only_that_a_key_exists(self):
        with patch.object(connections, "keyring_backend", return_value=(True, "test")):
            connections.store_secret("notion", "tok")
        entry = [c for c in connections.listing()["connections"] if c["id"] == "notion"][0]
        self.assertTrue(entry["has_secret"])
        self.assertNotIn("value", entry)
        self.assertNotIn("secret", entry)

    def test_a_machine_without_a_keyring_refuses_rather_than_improvising(self):
        # Writing a token to a file next to the code and calling it stored is
        # worse than saying no.
        with patch.object(connections, "keyring_backend", return_value=(False, "no keyring here")):
            with self.assertRaises(RuntimeError) as raised:
                connections.store_secret("notion", "tok")
        self.assertIn("no keyring here", str(raised.exception))
        self.assertEqual(self.secrets, {})

    def test_forgetting_takes_the_key_with_it(self):
        with patch.object(connections, "keyring_backend", return_value=(True, "test")):
            connections.store_secret("notion", "tok")
        connections.forget("notion")
        self.assertEqual(self.secrets, {})
        self.assertEqual(connections.listing()["connections"], [])

    def test_a_site_can_be_added_that_was_not_in_the_catalogue(self):
        entry = connections.add("example", sign_in_url="https://app.example.com/login")
        self.assertEqual(entry["kind"], "session")
        stored = self.store["connections"]["example"]
        self.assertEqual(stored["signed_in_host"], "app.example.com")


class SignedInTests(unittest.TestCase):
    """Where the browser lands is the signal, not what is drawn on the page."""

    def setUp(self):
        self.store = {"connections": {"google": dict(connections.CATALOG["google"])}}
        p = patch.object(connections, "_load", lambda: json.loads(json.dumps(self.store)))
        p.start(); self.addCleanup(p.stop)
        s = patch.object(connections, "_save", lambda d: self.store.update(d))
        s.start(); self.addCleanup(s.stop)

    def _check(self, landed):
        fake = type(sys)("browser")
        fake.run = lambda args: json.dumps({"ok": True, "url": landed})
        with patch.dict(sys.modules, {"browser": fake}):
            return connections.check("google")

    def test_staying_on_the_account_page_means_signed_in(self):
        self.assertEqual(self._check("https://myaccount.google.com/")["status"], "connected")

    def test_being_bounced_to_the_login_page_means_not(self):
        # Signed out, every service does the same thing, and that redirect is a
        # far steadier signal than any element on the page.
        out = self._check("https://accounts.google.com/ServiceLogin?continue=...")
        self.assertEqual(out["status"], "not connected")

    def test_a_service_that_logs_you_out_on_its_own_host_is_still_caught(self):
        self.store["connections"]["github"] = dict(connections.CATALOG["github"])
        fake = type(sys)("browser")
        fake.run = lambda args: json.dumps({"ok": True, "url": "https://github.com/login?return_to=x"})
        with patch.dict(sys.modules, {"browser": fake}):
            self.assertEqual(connections.check("github")["status"], "not connected")


class ConsentTests(unittest.TestCase):
    """Start sign-in on Olith's introduction page before opening the provider's login."""

    def setUp(self):
        self.store = {"connections": {"google": dict(connections.CATALOG["google"])}}
        p = patch.object(connections, "_load", lambda: json.loads(json.dumps(self.store)))
        p.start(); self.addCleanup(p.stop)
        s = patch.object(connections, "_save", lambda d: self.store.update(d))
        s.start(); self.addCleanup(s.stop)

    def _sign_in(self):
        opened = []
        fake = type(sys)("browser")
        fake.run = lambda args: opened.append(args) or json.dumps({"ok": True, "url": args["url"]})
        with patch.dict(sys.modules, {"browser": fake}):
            result = connections.sign_in("google")
        return opened, result

    def test_the_browser_opens_oliths_page_not_the_services(self):
        opened, _ = self._sign_in()
        self.assertEqual(len(opened), 1)
        self.assertIn("/connect/google", opened[0]["url"])
        self.assertNotIn("accounts.google.com", opened[0]["url"])

    def test_the_service_is_still_where_that_page_sends_you(self):
        # The consent page is an introduction, not a replacement: the sign-in
        # itself has to happen on the service's own page.
        self.assertEqual(
            connections.spec("google")["sign_in_url"],
            "https://accounts.google.com/ServiceLogin",
        )

    def test_the_consent_page_follows_the_port_the_server_is_on(self):
        with patch.dict(os.environ, {"OLITH_PORT": "7999"}):
            self.assertIn(":7999/connect/google", connections.consent_url("google"))

    def test_what_the_user_is_told_next_names_oliths_window(self):
        _, result = self._sign_in()
        self.assertIn("Olith opened its own browser window", result["next"])
        self.assertIn("never sees the password", result["next"])


class PromptTests(unittest.TestCase):
    def _block(self, entries):
        with patch.object(connections, "connected", return_value=entries):
            return connections.prompt_block()

    def test_nothing_connected_says_nothing(self):
        self.assertEqual(self._block([]), "")

    def test_sessions_and_keys_are_described_as_what_they_are(self):
        block = self._block([
            {"label": "Google", "kind": "session"},
            {"label": "Notion", "kind": "secret"},
        ])
        self.assertIn("browser is signed in to: Google", block)
        self.assertIn("holds a key for: Notion", block)

    def test_the_model_is_told_not_to_ask_for_a_password(self):
        block = self._block([{"label": "Google", "kind": "session"}])
        self.assertIn("never ask the user for a password", block)

    def test_a_key_is_named_and_never_valued(self):
        with patch.object(connections, "connected",
                          return_value=[{"label": "Notion", "kind": "secret"}]), \
             patch.object(connections, "secret", return_value="super-secret-token"):
            self.assertNotIn("super-secret-token", connections.prompt_block())


def _custom_session():
    return {
        "label": "Example", "kind": "session", "sign_in_url": "https://app.example.com/login",
        "check_url": "https://app.example.com/login", "signed_in_host": "app.example.com",
    }


def _store(**entries):
    return {"connections": {cid: {**spec, "status": "connected"} for cid, spec in entries.items()}}


class RouteContractTests(unittest.TestCase):
    """The prompt only promises a route that exists. This walks the data rather
    than naming services, so a new connection is covered the day it is added."""

    SESSIONS = {**{c: s for c, s in connections.CATALOG.items() if s["kind"] == "session"},
                "example": _custom_session()}
    KEYS = {**{c: {"label": c, "kind": "secret"} for c in connections.KEY_CONSUMERS},
            "notion": {"label": "Notion", "kind": "secret"}}

    def _with(self, store):
        p = patch.object(connections, "_load", lambda: json.loads(json.dumps(store)))
        p.start(); self.addCleanup(p.stop)

    def _promised_tools_exist(self, block, computer_use):
        offered = {t["function"]["name"] for t in agent.tool_defs_for({"computer_use": computer_use})}
        for tool in re.findall(r"`(\w+)`", block):
            self.assertIn(tool, offered, block)

    def test_every_session_connection_matches_the_browser_it_can_reach(self):
        for cid, spec in self.SESSIONS.items():
            with self.subTest(cid):
                self._with(_store(**{cid: spec}))
                on = connections.prompt_block(browser=True)
                self.assertIn(f"signed in to: {spec['label']}", on)
                self.assertIn("`browser`", on)
                self.assertIn("sign in again", on)
                self._promised_tools_exist(on, True)

                off = connections.prompt_block(browser=False)
                self.assertIn(spec["label"], off)
                self.assertIn("computer use", off)
                self.assertNotIn("signed in to", off)
                self.assertNotIn("Work in those", off)
                self._promised_tools_exist(off, False)

    def test_every_key_names_its_tool_or_says_it_has_none(self):
        for cid, spec in self.KEYS.items():
            for computer_use in (True, False):
                with self.subTest(cid, computer_use=computer_use):
                    self._with(_store(**{cid: spec}))
                    block = connections.prompt_block(browser=computer_use)
                    self.assertIn(spec["label"], block)
                    self._promised_tools_exist(block, computer_use)
                    tool = connections.KEY_CONSUMERS.get(cid)
                    if tool:
                        self.assertIn(f"`{tool}`", block)
                        self.assertNotIn("not usable", block)
                    else:
                        self.assertIn("not usable by any tool", block)
                        self.assertNotIn("the tool that uses it", block)
                        self.assertNotIn("signs in with it", block)

    def test_the_consumer_table_is_exactly_what_the_code_reads(self):
        root = Path(connections.__file__).parent
        source = "\n".join(p.read_text(encoding="utf-8") for p in root.glob("*.py"))
        reads = re.findall(r"connections\.secret\(([^)]*)\)", source)
        self.assertEqual({r.strip("\"'") for r in reads}, set(connections.KEY_CONSUMERS))

    def test_every_session_login_page_is_recognised_and_its_home_is_not(self):
        for cid, spec in self.SESSIONS.items():
            with self.subTest(cid):
                self._with(_store(**{cid: spec}))
                if spec["sign_in_url"] == spec.get("check_url") and spec.get("confirmation_required"):
                    self.assertTrue(spec.get("confirmation_required"))
                    continue
                note = connections.expired_sign_in_note(spec["sign_in_url"])
                self.assertIn("expired", note or "")
                self.assertIn("sign in again", note or "")
                self.assertIsNone(
                    connections.expired_sign_in_note(f"https://{spec['signed_in_host']}/"))


class ExpiredSignInTests(unittest.TestCase):
    def setUp(self):
        self.store = _store(github=connections.CATALOG["github"], google=connections.CATALOG["google"])
        p = patch.object(connections, "_load", lambda: json.loads(json.dumps(self.store)))
        p.start(); self.addCleanup(p.stop)

    def _visit(self, landed):
        page = json.dumps({"ok": True, "url": landed, "elements": []})
        with patch.object(agent.browser, "run", return_value=page):
            return json.loads(agent.run_tool("browser", {"action": "snapshot"}, None,
                                             chat={"computer_use": True}))

    def test_landing_on_a_login_page_tells_the_model_to_ask(self):
        out = self._visit("https://accounts.google.com/v3/signin/identifier?continue=x")
        self.assertIn("Google sign-in has expired", out["sign_in_expired"])
        self.assertTrue(out["ok"])

    def test_a_signed_out_path_on_the_services_own_host_counts(self):
        self.assertIn("GitHub", self._visit("https://github.com/login?return_to=x")["sign_in_expired"])

    def test_an_ordinary_page_says_nothing(self):
        for url in ("https://github.com/login-rs/login", "https://docs.google.com/document/d/1",
                    "https://example.org/login", "about:blank"):
            self.assertNotIn("sign_in_expired", self._visit(url), url)

    def test_a_service_never_connected_is_not_called_expired(self):
        self.store["connections"]["github"]["status"] = "not connected"
        self.assertNotIn("sign_in_expired", self._visit("https://github.com/login"))

    def test_a_custom_site_on_a_bare_address_cannot_be_told_apart_and_stays_quiet(self):
        self.store["connections"]["bare"] = {
            "kind": "session", "status": "connected",
            "sign_in_url": "https://bare.example.com/", "signed_in_host": "bare.example.com",
        }
        self.assertNotIn("sign_in_expired", self._visit("https://bare.example.com/anything"))

    def test_the_stored_status_is_left_to_check(self):
        before = json.dumps(self.store, sort_keys=True)
        self._visit("https://github.com/login")
        self.assertEqual(json.dumps(self.store, sort_keys=True), before)


class ComputerUseOffTests(unittest.TestCase):
    def setUp(self):
        self.store = _store(google=connections.CATALOG["google"],
                            **{"github-api": {"label": "GitHub API", "kind": "secret"}})
        p = patch.object(connections, "_load", lambda: json.loads(json.dumps(self.store)))
        p.start(); self.addCleanup(p.stop)

    def _system(self, **chat):
        chat = {"id": "c", "mode": "agentic", "project_id": None,
                "messages": [{"role": "user", "content": "hi"}], **chat}
        return server._build_messages(chat, "agent")[0]["content"]

    def test_an_agent_chat_with_it_off_is_told_why_and_keeps_the_key_route(self):
        system = self._system()
        self.assertIn("turn computer use on", system)
        self.assertIn("`git_status`", system)
        self.assertNotIn("signed in to: Google", system)
        self.assertNotIn("browser", {t["function"]["name"] for t in agent.tool_defs_for({})})

    def test_a_chat_with_it_on_is_told_the_account_is_live(self):
        with patch.object(server.computer, "available", return_value=(True, "")):
            self.assertIn("signed in to: Google", self._system(computer_use=True))

    def test_a_sub_agent_and_a_plain_chat_hear_nothing(self):
        self.assertNotIn("# Connections", self._system(subagent={"task": "look"}))
        self.assertNotIn("# Connections", self._system(mode="chat"))

    def test_building_a_prompt_never_touches_the_keyring(self):
        # Every chat builds this on every step, and a locked keyring can block.
        with patch.object(connections, "secret", side_effect=AssertionError("keyring read")), \
             patch.object(connections, "keyring_backend", side_effect=AssertionError("keyring read")):
            self.assertIn("GitHub API", connections.prompt_block(browser=False))


class ExpandedCatalogTests(unittest.TestCase):
    def setUp(self):
        self.store = {"connections": {}}
        for patcher in (patch.object(connections, "_load", side_effect=lambda: json.loads(json.dumps(self.store))),
                        patch.object(connections, "_save", side_effect=lambda data: self.store.update(data))):
            patcher.start(); self.addCleanup(patcher.stop)

    def test_catalog_has_categories_and_railway_and_preserves_existing_providers(self):
        self.assertGreaterEqual(len(connections.CATALOG), 34)
        for cid in ("google", "microsoft", "github", "railway", "slack", "notion", "gitlab"):
            self.assertIn(cid, connections.CATALOG)
        self.assertTrue(connections.CATALOG["railway"]["confirmation_required"])
        self.assertTrue(all(item.get("category") for item in connections.listing()["available"]))

    def test_new_sites_require_explicit_confirmation_after_checking(self):
        import browser
        from connection_catalog import BROWSER_CATALOG
        for cid, spec in BROWSER_CATALOG.items():
            with self.subTest(cid=cid):
                connections.add(cid)
                with patch.object(browser, "run", return_value=json.dumps({"url": spec["check_url"]})):
                    checked = connections.check(cid)
                self.assertEqual(checked["status"], "confirm sign-in")
                self.assertNotIn(cid, [entry["id"] for entry in connections.connected()])
                self.assertIsNone(checked["connected_at"])
                with patch.object(browser, "status", return_value={"running": True, "url": spec["check_url"], "ready_state": "complete"}):
                    self.assertEqual(connections.confirm_sign_in(cid)["status"], "connected")

    def test_login_page_is_not_a_connected_account(self):
        import browser
        connections.add("railway")
        with self.assertRaises(ValueError):
            connections.confirm_sign_in("railway")
        with patch.object(browser, "run", return_value=json.dumps({"url": "https://railway.com/login"})):
            self.assertEqual(connections.check("railway")["status"], "not connected")
        with self.assertRaises(ValueError):
            connections.confirm_sign_in("railway")


class BrowserSessionFlowTests(unittest.TestCase):
    setUp = ExpandedCatalogTests.setUp

    def pending(self, cid="vercel"):
        connections.add(cid)
        return connections._mark(cid, status="signing in", started_at=time.time())

    def page(self, url, ready="complete"):
        return {"running": True, "url": url, "ready_state": ready}

    def test_return_from_vercel_login_needs_one_honest_browser_confirmation(self):
        import browser
        self.pending()
        with patch.object(browser, "status", return_value=self.page("https://vercel.com/dashboard")), \
             patch.object(browser, "run", side_effect=AssertionError("must not navigate while observing")):
            result = connections.session_status("vercel")
            self.assertEqual(result["status"], "confirm sign-in")
            self.assertIsNone(result["connected_at"])
            self.assertEqual(connections.connected(), [])
            confirmed = connections.confirm_sign_in("vercel")
        self.assertEqual(confirmed["verification"], "user confirmed")
        self.assertIn("service tools connect separately", confirmed["status_note"])
        self.assertEqual(confirmed["tool_preset"], "vercel-cli")

    def test_a_public_homepage_is_never_automatic_proof_of_vercel_sign_in(self):
        import browser
        self.pending()
        with patch.object(browser, "status", return_value=self.page("https://vercel.com/")):
            self.assertEqual(connections.session_status("vercel")["status"], "confirm sign-in")
        self.assertEqual(connections.connected(), [])

    def test_confirm_cannot_accept_a_session_that_has_returned_to_login(self):
        import browser
        self.pending()
        with patch.object(browser, "status", return_value=self.page("https://vercel.com/dashboard")):
            connections.session_status("vercel")
        with patch.object(browser, "status", return_value=self.page("https://vercel.com/login")):
            with self.assertRaises(ValueError):
                connections.confirm_sign_in("vercel")
        self.assertEqual(connections.connected(), [])

    def test_a_loading_page_does_not_complete_the_sign_in(self):
        import browser
        self.pending("google")
        with patch.object(browser, "status", return_value=self.page("https://myaccount.google.com/", "loading")):
            self.assertEqual(connections.session_status("google")["status"], "signing in")

    def test_a_late_observation_cannot_complete_a_restarted_login(self):
        import browser
        self.pending("google")
        def previous_page():
            connections._mark("google", status="signing in", started_at=time.time() + 1)
            return self.page("https://myaccount.google.com/")
        with patch.object(browser, "status", side_effect=previous_page):
            self.assertEqual(connections.session_status("google")["status"], "signing in")
        self.assertEqual(connections.connected(), [])

    def test_browser_forgetting_never_unlocks_the_system_keyring(self):
        self.pending()
        with patch.object(connections, "_forget_secret", side_effect=AssertionError("browser has no saved key")):
            self.assertTrue(connections.forget("vercel")["ok"])

    def test_github_public_pages_are_not_treated_as_protected_account_pages(self):
        import browser
        self.pending("github")
        with patch.object(browser, "status", return_value=self.page("https://github.com/")):
            self.assertEqual(connections.session_status("github")["status"], "signing in")
        with patch.object(browser, "status", return_value=self.page("https://github.com/settings/profile")):
            self.assertEqual(connections.session_status("github")["status"], "connected")

    def test_closed_and_abandoned_browser_sign_ins_have_a_retryable_end_state(self):
        import browser
        self.pending()
        with patch.object(browser, "status", return_value={"running": False}):
            self.assertEqual(connections.session_status("vercel")["status"], "not connected")
        self.pending()
        connections._mark("vercel", started_at=1)
        with patch.object(browser, "status", side_effect=AssertionError("expired attempt should not touch Chrome")):
            self.assertEqual(connections.session_status("vercel")["status"], "not connected")

    def test_listing_a_saved_key_does_not_block_on_a_locked_keyring(self):
        connections.add("test-key", kind="secret")
        connections._mark("test-key", status="connected")
        with patch.object(connections, "secret", side_effect=AssertionError("must not read keyring")):
            result = connections.listing()
        self.assertTrue(result["connections"][0]["has_secret"])


class BrowserRouteResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_and_check_can_serve_their_own_browser_requests(self):
        import asyncio
        import browser
        import httpx
        import threading
        loop = asyncio.get_running_loop()
        main_thread = threading.get_ident()
        store = {"connections": {"vercel": dict(connections.CATALOG["vercel"])}}
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            def navigate(args):
                self.assertNotEqual(threading.get_ident(), main_thread, "blocking browser work must leave the event loop")
                # Chrome fetching Olith's introduction while the POST is still
                # in flight used to deadlock the same event loop for 20 seconds.
                response = asyncio.run_coroutine_threadsafe(client.get("/connect/vercel"), loop).result(timeout=1)
                self.assertEqual(response.status_code, 200)
                return json.dumps({"ok": True, "url": "https://vercel.com/dashboard"})
            with patch.object(connections, "_load", side_effect=lambda: json.loads(json.dumps(store))), \
                 patch.object(connections, "_save", side_effect=lambda data: store.update(data)), \
                 patch.object(browser, "run", side_effect=navigate):
                for action in ("sign-in", "check"):
                    response = await asyncio.wait_for(client.post(f"/api/connections/vercel/{action}"), timeout=2)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertTrue(response.json()["ok"])


if __name__ == "__main__":
    unittest.main()
