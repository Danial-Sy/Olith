# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Connections: which accounts Aether may act in, and how it holds them.

The shape of this is a decision, not an implementation detail. A password box
for Google would look right and fail every time, because Google refuses
scripted sign-in from an instrumented browser; what works is the user signing
in themselves, once, in Aether's own window, after which the session lives in
that browser profile like any other. So a session connection stores no secret
at all, and the secrets that do exist - API keys, for services that work that
way - go to the operating system's keyring and are never rendered anywhere the
model or a web page could read them.
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import connections


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
    """A sign-in starts on Aether's own page, never on the service's.

    A browser window the user did not open, landing straight on somebody's
    login form, says nothing about who opened it or what is being asked. That
    is the shape of a phishing page, and in use it was worse than that: after
    the Google sign-in, Chrome's own account machinery carried the window off
    to a third party's OAuth screen, and nothing on screen had ever said Aether.
    """

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

    def test_the_browser_opens_aethers_page_not_the_services(self):
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
        with patch.dict(os.environ, {"AETHER_PORT": "7999"}):
            self.assertIn(":7999/connect/google", connections.consent_url("google"))

    def test_what_the_user_is_told_next_names_aethers_window(self):
        _, result = self._sign_in()
        self.assertIn("Aether opened its own browser window", result["next"])
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


if __name__ == "__main__":
    unittest.main()
