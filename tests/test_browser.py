# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Test browser actions, covered targets, text insertion and shared editing guards.

The real DevTools protocol is checked by scripts/browser_smoke.py.
"""
import json
import os
import sys
import unittest
import unittest.mock
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent
import browser
import guards


class ActionTests(unittest.TestCase):
    def test_user_sign_in_focuses_the_window_without_waiting_for_page_resources(self):
        with patch.object(browser, "available", return_value=(True, "")), \
             patch.object(browser, "cmd", return_value={}) as command, \
             patch.object(browser, "_wait_loaded") as wait, patch.object(browser, "evaluate") as evaluate:
            out = json.loads(browser.run({"action": "open", "url": "http://127.0.0.1:7878/connect/vercel", "focus": True, "wait": False}))
        self.assertTrue(out["ok"])
        self.assertEqual(out["state"], "loading")
        self.assertEqual([call.args[0] for call in command.call_args_list], ["Page.bringToFront", "Page.navigate"])
        wait.assert_not_called(); evaluate.assert_not_called()

    def test_normal_browser_navigation_still_waits_and_reports_the_loaded_url(self):
        with patch.object(browser, "available", return_value=(True, "")), \
             patch.object(browser, "cmd", return_value={}), \
             patch.object(browser, "_wait_loaded", return_value="complete") as wait, \
             patch.object(browser, "evaluate", return_value={"url": "https://example.org/", "title": "Example"}):
            out = json.loads(browser.run({"action": "open", "url": "https://example.org"}))
        wait.assert_called_once()
        self.assertEqual(out["url"], "https://example.org/")

    def test_navigation_errors_cannot_report_a_successful_sign_in_window(self):
        with patch.object(browser, "available", return_value=(True, "")), \
             patch.object(browser, "cmd", return_value={"errorText": "net::ERR_CONNECTION_REFUSED"}):
            out = json.loads(browser.run({"action": "open", "url": "http://127.0.0.1:7878/connect/vercel", "wait": False}))
        self.assertIn("ERR_CONNECTION_REFUSED", out["error"])
        self.assertNotIn("ok", out)

    def test_an_unknown_action_is_refused_with_the_list(self):
        out = json.loads(browser.run({"action": "teleport"}))
        self.assertIn("Unknown browser action", out["error"])
        self.assertIn("snapshot", out["hint"])

    def test_acting_on_an_element_needs_a_ref(self):
        with patch.object(browser, "available", return_value=(True, "")):
            out = json.loads(browser.run({"action": "click"}))
        self.assertIn("needs a ref", out["error"])
        self.assertIn("snapshot", out["hint"])

    def test_an_unavailable_browser_says_why(self):
        with patch.object(browser, "binary", return_value=None):
            out = json.loads(browser.run({"action": "snapshot"}))
        self.assertIn("no Chrome-family browser", out["error"])


class RefTests(unittest.TestCase):
    """A ref is a promise that the click lands on the thing it names."""

    def _run(self, args, located):
        with patch.object(browser, "available", return_value=(True, "")), \
             patch.object(browser, "evaluate", return_value=located), \
             patch.object(browser, "_mouse") as mouse:
            out = json.loads(browser.run(args))
        return out, mouse

    def test_a_stale_ref_says_to_snapshot_again(self):
        out, mouse = self._run({"action": "click", "ref": "e9"}, {"missing": True})
        self.assertIn("No element e9", out["error"])
        self.assertIn("new snapshot", out["hint"])
        mouse.assert_not_called()

    def test_a_covered_element_is_not_clicked_through(self):
        # Clicking anyway is how a run ends up dismissing a cookie banner it
        # never saw, and reporting that it pressed Bold.
        out, mouse = self._run(
            {"action": "click", "ref": "e3"},
            {"x": 10, "y": 20, "covered": True, "over": "Cookie banner", "name": "Bold"},
        )
        self.assertIn("behind something else", out["error"])
        self.assertIn("Cookie banner", out["error"])
        mouse.assert_not_called()

    def test_a_clear_element_is_clicked_where_it_is(self):
        out, mouse = self._run(
            {"action": "click", "ref": "e3"},
            {"x": 10, "y": 20, "covered": False, "name": "Bold"},
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["clicked"], "Bold")
        mouse.assert_called_once_with(10, 20)


class TabPinTests(unittest.TestCase):
    """Keep driving Olith's chosen tab when Chrome opens other tabs."""

    def setUp(self):
        browser._target_id = None
        self.addCleanup(setattr, browser, "_target_id", None)

    def test_the_first_page_becomes_the_pinned_one(self):
        with patch.object(browser, "_targets", return_value=[
            {"id": "T1", "type": "page", "url": "about:blank"},
        ]):
            self.assertEqual(browser._page()["id"], "T1")
        self.assertEqual(browser._target_id, "T1")

    def test_the_pinned_tab_is_the_one_driven(self):
        browser._target_id = "T2"
        with patch.object(browser, "_targets", return_value=[
            {"id": "T1", "type": "page", "url": "https://claude.ai/oauth/authorize"},
            {"id": "T2", "type": "page", "url": "https://accounts.google.com/"},
        ]):
            # T1 is first and is not Olith's. Driving it would navigate the
            # user's own tab out from under them.
            self.assertEqual(browser._page()["id"], "T2")

    def test_a_closed_tab_is_replaced_and_the_socket_dropped(self):
        browser._target_id = "GONE"
        with patch.object(browser, "_targets", return_value=[
            {"id": "T9", "type": "page", "url": "about:blank"},
        ]), patch.object(browser, "_drop") as dropped:
            self.assertEqual(browser._page()["id"], "T9")
        dropped.assert_called_once()
        self.assertEqual(browser._target_id, "T9")

    def test_devtools_pages_are_not_candidates(self):
        with patch.object(browser, "_targets", return_value=[
            {"id": "D", "type": "page", "url": "devtools://devtools/bundled/x.html"},
            {"id": "R", "type": "page", "url": "https://example.com"},
        ]):
            self.assertEqual(browser._page()["id"], "R")


class CoveredElementTests(unittest.TestCase):
    """Check several points on an element before reporting it covered."""

    def test_the_element_is_clicked_at_a_point_where_it_is_reachable(self):
        # Centre blocked, a point above it clear: that is a widget's own
        # wrapper, not a banner over the page.
        with patch.object(browser, "available", return_value=(True, "")), \
             patch.object(browser, "evaluate",
                          return_value={"x": 40, "y": 12, "covered": False, "name": "Bold"}), \
             patch.object(browser, "_mouse") as mouse, \
             patch.object(browser, "cmd", return_value={}):
            out = json.loads(browser.run({"action": "click", "ref": "e5"}))
        self.assertTrue(out["ok"])
        mouse.assert_called_once_with(40, 12)

    def test_the_locator_tries_more_than_the_middle(self):
        # The script itself is what decides this, so the script is what is
        # checked: a single sampled point is the bug.
        self.assertIn("spots", browser._LOCATE_JS)
        self.assertGreaterEqual(browser._LOCATE_JS.count("[0."), 5)

    def test_a_real_overlay_is_still_refused(self):
        with patch.object(browser, "available", return_value=(True, "")), \
             patch.object(browser, "evaluate", return_value={
                 "x": 5, "y": 5, "covered": True, "over": "Cookie banner", "name": "Bold"}), \
             patch.object(browser, "_mouse") as mouse:
            out = json.loads(browser.run({"action": "click", "ref": "e5"}))
        self.assertIn("behind something else", out["error"])
        mouse.assert_not_called()


class WorldMovedTests(unittest.TestCase):
    """Advance the browser change counter so valid repeat actions can run."""

    def _run(self, args, **patches):
        defaults = dict(
            available=(True, ""),
        )
        with patch.object(browser, "available", return_value=(True, "")), \
             patch.object(browser, "cmd", return_value={}), \
             patch.object(browser, "_send_key"), \
             patch.object(browser, "_mouse"), \
             patch.object(browser, "evaluate",
                          return_value={"x": 1, "y": 1, "covered": False, "name": "x"}):
            return json.loads(browser.run(args))

    def test_a_click_moves_the_world(self):
        before = browser.state_id()
        self._run({"action": "click", "ref": "e1"})
        self.assertGreater(browser.state_id(), before)

    def test_typing_moves_the_world(self):
        before = browser.state_id()
        self._run({"action": "type", "text": "hello"})
        self.assertGreater(browser.state_id(), before)

    def test_looking_does_not(self):
        # A snapshot changes nothing, and saying it did would make every look
        # forgive the repeat that came before it.
        with patch.object(browser, "available", return_value=(True, "")), \
             patch.object(browser, "evaluate", return_value={"elements": [], "url": "u"}):
            before = browser.state_id()
            json.loads(browser.run({"action": "snapshot"}))
        self.assertEqual(browser.state_id(), before)


class NavigationTests(unittest.TestCase):
    def test_chromes_own_error_page_is_not_reported_as_loaded(self):
        # Chrome serves its error page and calls it complete. Passing that on
        # as ok hands the model a snapshot of Reload and Details buttons and
        # lets it believe it is on the site.
        with patch.object(browser, "available", return_value=(True, "")), \
             patch.object(browser, "cmd", return_value={}), \
             patch.object(browser, "_wait_loaded", return_value="complete"), \
             patch.object(browser, "evaluate",
                          return_value={"url": "chrome-error://chromewebdata/", "title": "x"}):
            out = json.loads(browser.run({"action": "open", "url": "nope.invalid"}))
        self.assertIn("did not load", out["error"])

    def test_a_bare_host_is_given_a_scheme(self):
        sent = {}
        with patch.object(browser, "available", return_value=(True, "")), \
             patch.object(browser, "cmd", side_effect=lambda m, **p: sent.update(p) or {}), \
             patch.object(browser, "_wait_loaded", return_value="complete"), \
             patch.object(browser, "evaluate",
                          return_value={"url": "https://docs.google.com/", "title": "Docs"}):
            json.loads(browser.run({"action": "open", "url": "docs.google.com"}))
        self.assertEqual(sent["url"], "https://docs.google.com")


class TypingTests(unittest.TestCase):
    def test_text_goes_in_as_one_insert(self):
        # The desktop path sends one synthetic keystroke per character, which is
        # eight seconds for a paragraph and a quarter of it lost. This is one
        # call whatever the length.
        calls = []
        with patch.object(browser, "available", return_value=(True, "")), \
             patch.object(browser, "cmd", side_effect=lambda m, **p: calls.append((m, p)) or {}):
            out = json.loads(browser.run({"action": "type", "text": "x" * 5000}))
        self.assertTrue(out["ok"])
        self.assertEqual(out["characters"], 5000)
        self.assertEqual([m for m, _ in calls], ["Input.insertText"])

    def test_a_key_repeat_is_capped(self):
        keys = []
        with patch.object(browser, "available", return_value=(True, "")), \
             patch.object(browser, "_send_key", side_effect=keys.append):
            # Not Return: that has its own budget now, and this is about the cap.
            out = json.loads(browser.run({"action": "key", "text": "Down", "count": 9999}))
        self.assertEqual(len(keys), 10)
        self.assertEqual(out["count"], 10)

    def test_a_shortcut_carries_its_modifiers(self):
        sent = []
        with patch.object(browser, "cmd", side_effect=lambda m, **p: sent.append(p) or {}):
            browser._send_key("ctrl+alt+1")
        # ctrl(2) | alt(1), on the Digit1 key: what a web application listens
        # for when it binds "heading 1".
        self.assertEqual(sent[0]["modifiers"], 3)
        self.assertEqual(sent[0]["code"], "Digit1")
        self.assertEqual(sent[0]["windowsVirtualKeyCode"], 49)


class SignInTests(unittest.TestCase):
    """Use Olith's own browser profile and never fill password fields."""

    def _snapshot(self, sign_in):
        payload = {"url": "https://accounts.google.com/", "title": "Sign in",
                   "elements": [{"ref": "e1", "role": "input", "name": "Password",
                                 "secret": True}],
                   "focused": None, "signIn": sign_in}
        with patch.object(browser, "available", return_value=(True, "")), \
             patch.object(browser, "evaluate", return_value=payload):
            return json.loads(browser.run({"action": "snapshot"}))

    def test_a_login_wall_is_reported_not_worked_around(self):
        out = self._snapshot(True)
        self.assertIn("sign in once in the Olith browser window",
                      out["sign_in_required"])
        self.assertIn("does not share the session", out["sign_in_required"])

    def test_an_ordinary_page_says_nothing_about_signing_in(self):
        self.assertNotIn("sign_in_required", self._snapshot(False))

    def test_a_password_field_is_never_filled(self):
        with patch.object(browser, "available", return_value=(True, "")), \
             patch.object(browser, "evaluate", side_effect=[
                 {"x": 5, "y": 5, "covered": False, "name": "Password"},  # locate
                 True,                                                     # is it secret
             ]), \
             patch.object(browser, "_mouse"), \
             patch.object(browser, "cmd") as sent:
            out = json.loads(browser.run({"action": "fill", "ref": "e1", "text": "hunter2"}))
        self.assertIn("password field", out["error"])
        sent.assert_not_called()

    def test_an_ordinary_field_is_still_filled(self):
        with patch.object(browser, "available", return_value=(True, "")), \
             patch.object(browser, "evaluate", side_effect=[
                 {"x": 5, "y": 5, "covered": False, "name": "Email"},
                 False, True, True,
             ]), \
             patch.object(browser, "_mouse"), \
             patch.object(browser, "cmd"):
            out = json.loads(browser.run({"action": "fill", "ref": "e1", "text": "a@b.c"}))
        self.assertTrue(out["ok"])


class SharedGuardTests(unittest.TestCase):
    """Apply the same document guards and selection state to both browser and desktop actions."""

    def setUp(self):
        guards.begin_run()
        self.addCleanup(guards.begin_run)

    def _run(self, args):
        with patch.object(browser, "available", return_value=(True, "")), \
             patch.object(browser, "cmd", return_value={}), \
             patch.object(browser, "_send_key"):
            return json.loads(browser.run(args))

    def test_return_after_select_all_is_refused_here_too(self):
        self._run({"action": "key", "text": "ctrl+a"})
        out = self._run({"action": "key", "text": "Return"})
        self.assertIn("replace all of it", out["error"])

    def test_typing_over_a_select_all_is_refused_here_too(self):
        self._run({"action": "key", "text": "ctrl+a"})
        self.assertIn("error", self._run({"action": "type", "text": "new document"}))

    def test_a_selection_made_in_one_tool_is_seen_by_the_other(self):
        # The state is shared on purpose: a selection does not stop existing
        # because the next call came through a different door.
        self._run({"action": "key", "text": "ctrl+a"})
        self.assertTrue(guards.state()["select_all"])
        refusal = guards.check("type", {"text": "x"})
        self.assertIn("replace all of it", refusal["error"])

    def test_the_blank_line_budget_applies_in_the_browser(self):
        for _ in range(guards.BLANK_LINE_BUDGET):
            self._run({"action": "key", "text": "Return"})
        self.assertIn("Returns with nothing typed", self._run({"action": "key", "text": "Return"})["error"])

    def test_filling_a_named_field_clears_the_selection(self):
        # `fill` puts the caret in one field: whatever was selected is not any
        # more, and a later Return is an ordinary keypress.
        self._run({"action": "key", "text": "ctrl+a"})
        guards.check("fill", {"text": "someone@example.com"})
        self.assertNotIn("error", self._run({"action": "key", "text": "Return"}))

    def test_formatting_and_deselecting_still_work(self):
        self._run({"action": "key", "text": "ctrl+a"})
        self.assertNotIn("error", self._run({"action": "key", "text": "ctrl+b"}))
        self._run({"action": "key", "text": "End"})
        self.assertNotIn("error", self._run({"action": "type", "text": "the next line"}))


class CanvasTests(unittest.TestCase):
    def test_a_page_that_draws_its_text_says_it_cannot_be_read(self):
        # The run that got stuck kept asking a canvas application for its text
        # and kept getting the toolbar back.
        with patch.object(browser, "available", return_value=(True, "")), \
             patch.object(browser, "evaluate", return_value={
                 "url": "https://docs.google.com/document/d/x/edit",
                 "title": "Essay", "canvas": True, "text": "File Edit View",
             }):
            out = json.loads(browser.run({"action": "read"}))
        self.assertIn("draws its content on a canvas", out["cannot_read"])
        self.assertIn("screenshot with a region", out["cannot_read"])

    def test_an_ordinary_page_is_just_read(self):
        with patch.object(browser, "available", return_value=(True, "")), \
             patch.object(browser, "evaluate", return_value={
                 "url": "https://example.com", "title": "Example",
                 "canvas": False, "text": "x" * 400,
             }):
            out = json.loads(browser.run({"action": "read"}))
        self.assertNotIn("cannot_read", out)


class WiringTests(unittest.TestCase):
    def test_the_tool_is_offered_with_computer_use_and_not_without(self):
        names = [t["function"]["name"] for t in agent.tool_defs_for({"computer_use": True})]
        self.assertIn("browser", names)
        self.assertNotIn("browser", [t["function"]["name"] for t in agent.tool_defs_for({})])

    def test_the_tool_refuses_to_run_for_a_chat_with_it_off(self):
        out = json.loads(agent.run_tool("browser", {"action": "status"}, None, chat={}))
        self.assertIn("off for this chat", out["error"])

    def test_a_sub_agent_never_gets_the_browser(self):
        names = [
            t["function"]["name"]
            for t in agent.tool_defs_for({"computer_use": True, "subagent": {"tools": ["browser"]}})
        ]
        self.assertNotIn("browser", names)

    def test_the_activity_line_says_what_it_did(self):
        self.assertIn("open https://docs.google.com",
                      agent.tool_activity_line("browser", {"action": "open",
                                                           "url": "https://docs.google.com"}))


if __name__ == "__main__":
    unittest.main()
