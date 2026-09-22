# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Computer use: the tool, its wiring, and the loop that keeps it honest.

What broke before was never the model's eyesight. It was that a screenshot went
nowhere, so acting and observing were disconnected: the agent typed a search,
had no way to see the results, and typed it again. These tests hold the joins
that fix that: a frame comes back as an image, an action that changed nothing
says so, and a run that only produces those is stopped rather than left to spin.

Nothing here needs a display. Capture and injection are covered by driving a
real widget on a nested X server (see the notes in `computer.py`); what is
tested here is everything that decides what the model is shown and allowed.
"""
import json
import os
import sys
import unittest
import unittest.mock
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent
import computer

try:
    import server
except ModuleNotFoundError:
    server = None


class CoordinateTests(unittest.TestCase):
    """The model is shown a downscaled frame and sends points on that frame."""

    def test_a_point_on_the_frame_maps_onto_the_display(self):
        # Whatever the frame is capped at, its midpoint is the display's
        # midpoint and not a click a thousand pixels to the left.
        self.assertAlmostEqual(
            computer._frame_scale(3440, 1440), computer.MAX_EDGE / 3440
        )
        half = computer.MAX_EDGE / 2
        x, y = computer.to_screen(half, half * 1440 / 3440, 3440, 1440)
        self.assertEqual((x, y), (1720, 720))

    def test_a_display_smaller_than_the_frame_cap_is_not_upscaled(self):
        self.assertEqual(computer._frame_scale(1280, 800), 1.0)
        self.assertEqual(computer.to_screen(640, 400, 1280, 800), (640, 400))

    def test_a_point_off_the_frame_is_clamped_onto_the_screen(self):
        self.assertEqual(computer.to_screen(99999, -20, 1920, 1080), (1919, 0))


class ChangeDetectionTests(unittest.TestCase):
    """Whether the screen responded is what tells an action from a no-op."""

    @staticmethod
    def _grid(fill=0, spot=None):
        cells = bytearray([fill]) * (computer._GRID[0] * computer._GRID[1])
        if spot is not None:
            cells[spot[0]] = spot[1]
        return bytes(cells)

    def test_an_identical_screen_is_not_a_change(self):
        self.assertFalse(computer._changed(self._grid(40), self._grid(40)))

    def test_one_cell_moving_is_a_change(self):
        # A button that only changed shade must not read as "nothing happened":
        # that is what sends the model back to click it again.
        self.assertTrue(
            computer._changed(self._grid(40), self._grid(40, spot=(500, 90)))
        )

    def test_noise_below_the_threshold_is_not_a_change(self):
        self.assertFalse(
            computer._changed(self._grid(40), self._grid(40, spot=(500, 42)))
        )

    def test_the_first_frame_of_a_session_counts_as_a_change(self):
        self.assertTrue(computer._changed(None, self._grid(40)))


class ActionValidationTests(unittest.TestCase):
    def test_an_unknown_action_is_refused_with_the_list(self):
        out = json.loads(computer.run({"action": "teleport"}))
        self.assertIn("Unknown action", out["error"])
        self.assertIn("screenshot", out["hint"])

    def test_a_click_without_coordinates_is_refused_before_it_runs(self):
        out = json.loads(computer.run({"action": "click"}))
        # Reached the argument check, or the machine has no desktop at all; both
        # are refusals rather than a click at (0, 0).
        self.assertIn("error", out)
        self.assertNotIn("ok", out)

    def test_a_repeat_count_is_capped(self):
        # A model handed `count` so it need not spam a key must not be able to
        # spam it inside one call instead. The driver is stubbed: an uncapped
        # count here would put thousands of real keystrokes into whatever window
        # happens to have focus.
        pressed = []

        class Recorder:
            def key(self, combo):
                pressed.append(combo)

        with patch.object(computer, "_backend", Recorder()), \
             patch.object(computer, "_available", (True, "")), \
             patch.object(computer, "_capture", return_value={
                 "w": 800, "h": 600, "id": 1, "b64": "", "zoom": 1.0, "origin": [0, 0],
             }):
            # Not Return: that has its own budget, and this is about the cap.
            out = json.loads(computer.run({"action": "key", "text": "Down", "count": 9999}))
        computer.pop_frame()
        self.assertEqual(len(pressed), 10)
        self.assertIn("x10", out["note"])

    def test_the_status_line_names_the_act_not_the_tool(self):
        self.assertEqual(
            computer.status_line({"action": "click", "x": 12, "y": 34}),
            "Aether is clicking at 12, 34",
        )
        self.assertIn("typing", computer.status_line({"action": "type", "text": "hi"}))
        self.assertIn("looking", computer.status_line({"action": "screenshot"}))


class SelectionTests(unittest.TestCase):
    """The move that deleted a finished two-page paper.

    Taken from the transcript of the run that did it: type the title, press
    ctrl+a meaning "select this line", press Return. ctrl+a is Select All, so
    Return replaced the whole document with a newline. The model did it twice
    more while trying to recover, and never noticed, because at 47% scale it
    could not read the page it had just emptied.
    """

    def setUp(self):
        computer.begin_run()
        self.addCleanup(computer.begin_run)

    def _run(self, args):
        # The desktop is never touched: this is about what the tool refuses
        # before it sends anything.
        with patch.object(computer, "_available", (True, "")), \
             patch.object(computer, "_backend", unittest.mock.MagicMock()), \
             patch.object(computer, "_capture", return_value={
                 "w": 800, "h": 600, "id": 1, "b64": "", "zoom": 1.0, "origin": [0, 0],
             }):
            out = json.loads(computer.run(args))
        computer.pop_frame()
        return out

    def test_return_after_select_all_is_refused(self):
        self._run({"action": "key", "text": "ctrl+a"})
        out = self._run({"action": "key", "text": "Return"})
        self.assertIn("replace all of it", out["error"])
        self.assertIn("shift+End", out["hint"])

    def test_typing_over_a_select_all_is_refused(self):
        self._run({"action": "key", "text": "ctrl+a"})
        out = self._run({"action": "type", "text": "new title"})
        self.assertIn("error", out)

    def test_formatting_a_selection_is_not_replacing_it(self):
        # ctrl+b on a select-all is the legitimate half of what that run was
        # trying to do, and it leaves the selection standing.
        self._run({"action": "key", "text": "ctrl+a"})
        self.assertNotIn("error", self._run({"action": "key", "text": "ctrl+b"}))
        self.assertIn("error", self._run({"action": "key", "text": "Return"}))

    def test_a_modifier_does_not_make_a_destructive_key_safe(self):
        # shift+Delete empties a selection exactly as Delete does, and alt+Tab
        # is not destructive at all: what decides is the key on the end.
        self._run({"action": "key", "text": "ctrl+a"})
        self.assertIn("error", self._run({"action": "key", "text": "shift+Delete"}))
        self.assertNotIn("error", self._run({"action": "key", "text": "alt+Tab"}))

    def test_moving_the_caret_drops_the_selection(self):
        self._run({"action": "key", "text": "ctrl+a"})
        self._run({"action": "key", "text": "ctrl+End"})
        self.assertNotIn("error", self._run({"action": "type", "text": "the next paragraph"}))

    def test_a_toolbar_click_does_not_drop_the_selection(self):
        # The exact sequence of the run that lost its paper the second time:
        # ctrl+a, six clicks at the font-size box trying to enlarge the title,
        # then Return. Clicking a toolbar button leaves the document selected,
        # so treating any click as a deselect stood the guard down right before
        # the keypress that mattered.
        self._run({"action": "key", "text": "ctrl+a"})
        for _ in range(6):
            self._run({"action": "click", "x": 424, "y": 151})
        self.assertIn("error", self._run({"action": "key", "text": "Return"}))

    def test_pressing_end_drops_the_selection(self):
        # The deselect the refusal actually tells it to use.
        self._run({"action": "key", "text": "ctrl+a"})
        self._run({"action": "key", "text": "End"})
        self.assertNotIn("error", self._run({"action": "type", "text": "hello"}))

    def test_a_deliberate_replacement_still_works(self):
        # Selecting a URL bar and typing over it is ordinary. The refusal asks
        # for intent, it does not remove the capability.
        self._run({"action": "key", "text": "ctrl+a"})
        out = self._run({
            "action": "type", "text": "docs.google.com", "confirm": "replace_selection",
        })
        self.assertNotIn("error", out)

    def test_after_a_confirmed_replacement_nothing_is_selected_any_more(self):
        # The typing consumed the selection, so the next Return is an ordinary
        # paragraph break and must not be refused as if the document were still
        # highlighted.
        self._run({"action": "key", "text": "ctrl+a"})
        self._run({"action": "type", "text": "x", "confirm": "replace_selection"})
        self.assertNotIn("error", self._run({"action": "key", "text": "Return"}))

    def test_a_fresh_select_all_needs_a_fresh_confirmation(self):
        self._run({"action": "key", "text": "ctrl+a"})
        self._run({"action": "type", "text": "x", "confirm": "replace_selection"})
        self._run({"action": "key", "text": "ctrl+a"})
        self.assertIn("error", self._run({"action": "key", "text": "Return"}))


class BlankLineTests(unittest.TestCase):
    """The other half of the same run: ctrl+End, Return, Return, Return, click,
    ctrl+End, Return... for 55 presses, trying to reach a place on a page it
    could not read."""

    def setUp(self):
        computer.begin_run()
        self.addCleanup(computer.begin_run)

    def _run(self, args):
        with patch.object(computer, "_available", (True, "")), \
             patch.object(computer, "_backend", unittest.mock.MagicMock()), \
             patch.object(computer, "_capture", return_value={
                 "w": 800, "h": 600, "id": 1, "b64": "", "zoom": 1.0, "origin": [0, 0],
             }):
            out = json.loads(computer.run(args))
        computer.pop_frame()
        return out

    def test_a_paragraph_break_is_fine(self):
        for _ in range(computer.BLANK_LINE_BUDGET):
            self.assertNotIn("error", self._run({"action": "key", "text": "Return"}))

    def test_past_the_budget_it_is_refused(self):
        for _ in range(computer.BLANK_LINE_BUDGET):
            self._run({"action": "key", "text": "Return"})
        out = self._run({"action": "key", "text": "Return"})
        self.assertIn("Returns with nothing typed", out["error"])
        self.assertIn("ctrl+End", out["hint"])

    def test_typing_something_earns_the_budget_back(self):
        for _ in range(computer.BLANK_LINE_BUDGET):
            self._run({"action": "key", "text": "Return"})
        self._run({"action": "type", "text": "The next section."})
        self.assertNotIn("error", self._run({"action": "key", "text": "Return"}))

    def test_a_count_cannot_be_used_to_get_around_it(self):
        out = self._run({"action": "key", "text": "Return", "count": 9})
        self.assertIn("error", out)


class TypingHonestyTests(unittest.TestCase):
    def test_the_tool_does_not_claim_the_characters_arrived(self):
        # It reported "Typed 664 characters" for a paragraph that reached the
        # page 166 characters short, and the model built on that claim.
        with patch.object(computer, "_available", (True, "")), \
             patch.object(computer, "_backend", unittest.mock.MagicMock()), \
             patch.object(computer, "_capture", return_value={
                 "w": 800, "h": 600, "id": 1, "b64": "", "zoom": 1.0, "origin": [0, 0],
             }):
            out = json.loads(computer.run({"action": "type", "text": "hello there"}))
        computer.pop_frame()
        computer.begin_run()
        self.assertNotIn("Typed", out["note"])
        self.assertIn("Sent 11 characters", out["note"])
        self.assertIn("actually landed", out["note"])


class LegibilityTests(unittest.TestCase):
    def test_a_wide_display_says_its_frame_cannot_be_read(self):
        # 3440x1440 scales to 58%, and 11pt body text with it. A run that
        # believes it can read the page from that frame writes into a document
        # it cannot see.
        self.assertLess(computer._frame_scale(3440, 1440), computer.LEGIBLE_SCALE)
        with patch.object(computer, "_available", (True, "")), \
             patch.object(computer, "_backend", unittest.mock.MagicMock()), \
             patch.object(computer, "screen_size", return_value=(3440, 1440)), \
             patch.object(computer, "_capture", return_value={
                 "w": 2000, "h": 837, "id": 2, "b64": "", "zoom": 1.0, "origin": [0, 0],
             }):
            out = json.loads(computer.run({"action": "screenshot"}))
        computer.pop_frame()
        self.assertIn("region", out["legibility"])

    def test_an_ordinary_display_is_not_nagged(self):
        self.assertGreaterEqual(computer._frame_scale(1920, 1080), computer.LEGIBLE_SCALE)


class BatchTests(unittest.TestCase):
    """Several steps on one model turn.

    The measured cost of a desktop job was 15.5s a step, of which 0.7s was the
    action: what a run costs is how many times the model has to look and decide.
    A heading is six keystrokes and no decisions in between, so it should cost
    one turn, not six.
    """

    def setUp(self):
        computer.begin_run()
        self.addCleanup(computer.begin_run)
        self.driver = unittest.mock.MagicMock()

    def _run(self, args, pause=0):
        # The capture is a mock so the test can count it: "one frame per call"
        # is the claim, and popping the frame here would destroy the evidence
        # for it.
        self.capture = unittest.mock.MagicMock(return_value={
            "w": 1920, "h": 1080, "id": 1, "b64": "", "zoom": 1.0, "origin": [0, 0],
        })
        self.slept = []
        with patch.object(computer, "_available", (True, "")), \
             patch.object(computer, "_backend", self.driver), \
             patch.object(computer, "STEP_PAUSE", pause), \
             patch.object(computer, "SETTLE", 0), \
             patch.object(computer.time, "sleep", self.slept.append), \
             patch.object(computer, "screen_size", return_value=(1920, 1080)), \
             patch.object(computer, "_capture", self.capture):
            out = json.loads(computer.run(args))
        computer.pop_frame()
        return out

    HEADING = [
        {"action": "key", "text": "ctrl+alt+1"},
        {"action": "key", "text": "ctrl+shift+e"},
        {"action": "type", "text": "The Title"},
        {"action": "key", "text": "Return"},
        {"action": "key", "text": "ctrl+alt+0"},
        {"action": "type", "text": "The body."},
    ]

    def test_a_batch_runs_every_step_in_order(self):
        out = self._run({"actions": self.HEADING})
        self.assertTrue(out["ok"])
        self.assertEqual(out["actions_run"], "6 of 6")
        self.assertEqual(
            [c[0] for c in self.driver.method_calls],
            ["key", "key", "type_text", "key", "key", "type_text"],
        )

    def test_one_frame_comes_back_however_many_steps_ran(self):
        # The screenshot is what the next decision is made from. The states in
        # the middle of a sequence the model already chose are not decisions,
        # and at ~1.6k tokens each they are what would fill the window.
        self._run({"actions": self.HEADING})
        self.assertEqual(self.capture.call_count, 1)

    def test_steps_are_paced_so_the_screen_can_keep_up(self):
        # A click on a menu and a click on its item are one batch, and the
        # second lands on nothing if the first has not painted yet.
        self._run({"actions": self.HEADING}, pause=0.25)
        self.assertEqual(
            self.slept.count(0.25), len(self.HEADING) - 1,
            "steps ran back to back with no pause between them",
        )

    def test_a_refused_step_stops_the_rest(self):
        out = self._run({"actions": [
            {"action": "type", "text": "a title"},
            {"action": "key", "text": "ctrl+a"},
            {"action": "key", "text": "Return"},
            {"action": "type", "text": "this must not be sent"},
        ]})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stopped_at"], 2)
        self.assertEqual(out["actions_run"], "2 of 4")
        self.assertIn("replace all of it", out["error"])
        typed = [c for c in self.driver.method_calls if c[0] == "type_text"]
        self.assertEqual(len(typed), 1, "a step after the refusal still reached the desktop")

    def test_the_report_says_what_did_run(self):
        out = self._run({"actions": [
            {"action": "key", "text": "ctrl+a"},
            {"action": "key", "text": "Return"},
        ]})
        self.assertEqual(out["actions_run"], "1 of 2")
        self.assertIn("Actions 0-0 ran", out["note"])

    def test_a_first_step_refused_returns_no_frame(self):
        # Nothing reached the desktop, so the screen is the one the model is
        # already looking at; another copy of it costs tokens to say nothing.
        self._run({"action": "key", "text": "ctrl+a"})
        out = self._run({"actions": [
            {"action": "key", "text": "Return"},
            {"action": "type", "text": "x"},
        ]})
        self.assertEqual(out["actions_run"], "0 of 2")
        self.assertEqual(self.capture.call_count, 0)

    def test_an_overlong_batch_is_refused_whole(self):
        out = self._run({"actions": [{"action": "key", "text": "a"}] * (computer.MAX_BATCH + 1)})
        self.assertIn("the limit is", out["error"])
        self.assertEqual(self.driver.method_calls, [])

    def test_a_single_action_still_reads_as_one_action(self):
        out = self._run({"action": "key", "text": "ctrl+b"})
        self.assertEqual(out["action"], "key")
        self.assertNotIn("performed", out)

    def test_the_overlay_names_what_a_batch_is_doing(self):
        self.assertIn("typing", computer.status_line({"actions": self.HEADING}))
        self.assertIn(
            "3 steps",
            computer.status_line({"actions": [{"action": "key", "text": "Tab"}] * 3}),
        )


class WidgetRefTests(unittest.TestCase):
    """Aiming at a named widget instead of a guessed pixel.

    The run that could not press Bold clicked six times at the same wrong spot,
    because the only way it had to say "that button" was to estimate its
    coordinates from an image scaled to 58%. A ref carries the real box.
    """

    def setUp(self):
        computer.begin_run()
        self.addCleanup(computer.begin_run)
        self.driver = unittest.mock.MagicMock()

    def _run(self, args):
        with patch.object(computer, "_available", (True, "")), \
             patch.object(computer, "_backend", self.driver), \
             patch.object(computer, "SETTLE", 0), \
             patch.object(computer.time, "sleep", lambda *_: None), \
             patch.object(computer, "screen_size", return_value=(3440, 1440)), \
             patch.object(computer, "_capture", return_value={
                 "w": 2000, "h": 837, "id": 1, "b64": "", "zoom": 1.0, "origin": [0, 0],
             }):
            out = json.loads(computer.run(args))
        computer.pop_frame()
        return out

    def test_a_ref_clicks_the_middle_of_the_real_widget(self):
        # 2400,300 is the centre of the box, in screen pixels. Going through the
        # frame would have put it at 4137,517, off the display.
        computer._refs["a4"] = (2300, 280, 200, 40, "Bold", "push button")
        out = self._run({"action": "click", "ref": "a4"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["clicked"], "Bold")
        self.driver.click.assert_called_once_with(2400, 300, button="left")

    def test_an_unknown_ref_is_refused_before_anything_is_clicked(self):
        out = self._run({"action": "click", "ref": "a99"})
        self.assertIn("No widget a99", out["error"])
        self.driver.click.assert_not_called()

    def test_refs_do_not_survive_a_new_run(self):
        # A window from the last job is not the window in front of this one.
        computer._refs["a1"] = (0, 0, 10, 10, "Old", "push button")
        computer.begin_run()
        self.assertIn("error", self._run({"action": "click", "ref": "a1"}))

    @staticmethod
    def _node(name, role, box, children=()):
        """Enough of an AT-SPI accessible for the walk to read."""
        node = unittest.mock.MagicMock()
        node.get_name.return_value = name
        node.get_role_name.return_value = role
        extents = unittest.mock.MagicMock(x=box[0], y=box[1], width=box[2], height=box[3])
        node.get_component_iface.return_value.get_extents.return_value = extents
        node.get_child_count.return_value = len(children)
        node.get_child_at_index.side_effect = lambda i: children[i]
        return node

    def test_a_widget_reporting_a_box_outside_its_window_is_dropped(self):
        # Window-manager title-bar buttons come back at (0, 0, 22, 22) whatever
        # the window's position, and a ref aiming at the corner of the display
        # is worse than no ref.
        window = (600, 300, 800, 500)
        self.assertTrue(computer._inside((700, 400, 60, 20), window))
        self.assertFalse(computer._inside((0, 0, 22, 22), window))

        liar = self._node("Close", "push button", (0, 0, 22, 22))
        real = self._node("Bold", "push button", (700, 400, 60, 20))
        frame = self._node("Editor", "frame", window, (liar, real))
        found: list = []
        computer._walk(frame, unittest.mock.MagicMock(), found)
        self.assertEqual([name for name, _role, _box in found], ["Editor", "Bold"])

    def test_positions_are_reported_in_the_screenshots_coordinates(self):
        # The two ways of looking at the screen have to agree, or a model
        # reading `at` will not find that widget in the image.
        atspi = unittest.mock.MagicMock()
        with patch.object(computer, "_atspi", return_value=atspi), \
             patch.object(computer, "_active_frames", return_value=[object()]), \
             patch.object(computer, "screen_size", return_value=(3440, 1440)), \
             patch.object(computer, "_walk",
                          side_effect=lambda n, a, out, *r, **k: out.append(
                              ("Bold", "push button", (1000, 500, 100, 40)))):
            found = computer.elements()
        scale = computer._frame_scale(3440, 1440)
        self.assertEqual(found["elements"][0]["at"],
                         [int(1050 * scale), int(520 * scale)])

    def test_a_desktop_without_an_accessibility_bus_says_so(self):
        with patch.object(computer, "_atspi", return_value=None):
            found = computer.elements()
        self.assertIn("No accessibility bus", found["error"])
        self.assertIn("browser tool", found["hint"])


class ToolWiringTests(unittest.TestCase):
    def test_the_schema_is_only_offered_to_a_chat_that_asked_for_it(self):
        names = [t["function"]["name"] for t in agent.tool_defs_for({"computer_use": True})]
        self.assertIn("computer", names)
        self.assertNotIn("computer", [t["function"]["name"] for t in agent.tool_defs_for({})])

    def test_the_tool_refuses_to_run_for_a_chat_with_it_off(self):
        # Withholding a schema is advice, not enforcement: a model that names
        # the tool anyway must not be handed the mouse.
        out = json.loads(agent.run_tool("computer", {"action": "screenshot"}, None, chat={}))
        self.assertIn("off for this chat", out["error"])

    def test_a_sub_agent_never_gets_the_desktop(self):
        names = [
            t["function"]["name"]
            for t in agent.tool_defs_for({"computer_use": True, "subagent": {"tools": ["computer"]}})
        ]
        self.assertNotIn("computer", names)

    def test_an_alias_keeps_the_action_it_named(self):
        # `press_key` says which action it wants; canonicalising to `computer`
        # alone would throw that away, and nothing in the arguments tells a
        # hotkey from typing since both carry only `text`.
        call = agent.extract_tool_calls(
            {"tool_calls": [{"function": {"name": "press_key", "arguments": '{"text": "ctrl+l"}'}}]}
        )[0]
        self.assertEqual(call["name"], "computer")
        self.assertEqual(call["arguments"], {"text": "ctrl+l", "action": "key"})

    def test_an_explicit_action_survives_an_alias(self):
        args = agent.apply_alias_defaults("screenshot", "computer", {"action": "click", "x": 1})
        self.assertEqual(args["action"], "click")

    def test_the_activity_line_reads_as_an_action(self):
        line = agent.tool_activity_line("computer", {"action": "type", "x": 4, "y": 9, "text": "hi"})
        self.assertIn("type at 4,9", line)


@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class FrameDeliveryTests(unittest.TestCase):
    """A screenshot has to reach the model as an image, and then get out of the
    way before it fills the window."""

    @staticmethod
    def _frame(n):
        return {"b64": f"IMAGE{n}", "w": 1600, "h": 669, "id": n}

    def _chat(self, frames):
        msgs = [{"role": "user", "content": "search the web for gorillas"}]
        for i in range(frames):
            msgs.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{"name": "computer", "arguments": {"action": "screenshot"}}],
            })
            msgs.append({"role": "tool", "name": "computer", "content": '{"ok": true}'})
            msgs.append(server._frame_message(self._frame(i)))
        return {"id": "cu", "mode": "agentic", "computer_use": True, "messages": msgs}

    def test_a_frame_rides_as_a_user_message_carrying_the_image(self):
        msg = server._frame_message(self._frame(1))
        self.assertEqual(msg["role"], "user")
        self.assertEqual(msg["images"], ["IMAGE1"])
        self.assertTrue(msg["quiet"], "frames are machinery, not transcript")

    def test_only_the_newest_frames_are_sent_as_pixels(self):
        msgs = server._build_messages(self._chat(5), "vision-model")
        carried = [m for m in msgs if m.get("images")]
        self.assertEqual(len(carried), server.COMPUTER_FRAME_KEEP)
        self.assertEqual(carried[-1]["images"], ["IMAGE4"])
        # The older looks still appear, as captions, so the sequence reads.
        self.assertEqual(
            len([m for m in msgs if (m.get("content") or "").startswith("[screen]")]), 5
        )

    def test_a_frame_does_not_start_a_new_user_turn(self):
        # The run boundary is the last thing the user actually said. A frame
        # counted as a user turn would cut the job's own history off from it.
        chat = self._chat(2)
        self.assertEqual(
            server._current_run_messages(chat)[0]["content"], "search the web for gorillas"
        )

    def test_superseded_frames_stop_being_stored(self):
        chat = self._chat(4)
        server._prune_stored_frames(chat)
        stored = [m for m in chat["messages"] if m.get("computer_frame") and m.get("images")]
        self.assertEqual(len(stored), server.COMPUTER_FRAME_KEEP)


@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class LoopGuardTests(unittest.TestCase):
    def test_a_repeat_against_an_unchanged_screen_is_still_a_repeat(self):
        call = {"name": "computer", "arguments": {"action": "click", "x": 4, "y": 5}}
        with patch.object(computer, "frame_id", return_value=7):
            first = server._call_signature(call, None)
            same = server._call_signature(call, None)
        self.assertEqual(first, same)

    def test_the_same_action_on_a_changed_screen_is_a_new_call(self):
        # Clicking the same button twice is ordinary work. Keyed on arguments
        # alone the guard would refuse to run it a third time.
        call = {"name": "computer", "arguments": {"action": "click", "x": 4, "y": 5}}
        with patch.object(computer, "frame_id", return_value=7):
            before = server._call_signature(call, None)
        with patch.object(computer, "frame_id", return_value=8):
            after = server._call_signature(call, None)
        self.assertNotEqual(before, after)

    def test_actions_that_change_nothing_end_the_run_rather_than_repeat(self):
        stalls = 0
        dead = json.dumps({"ok": True, "action": "click", "screen_changed": False})
        for _ in range(server.COMPUTER_STALL_LIMIT - 1):
            out, stalls = server._computer_progress(dead, stalls)
            self.assertNotIn("error", json.loads(out))
        out, stalls = server._computer_progress(dead, stalls)
        payload = json.loads(out)
        self.assertIn("left the screen unchanged", payload["error"])
        self.assertIn("Stop acting now", payload["hint"])

    def test_one_action_that_lands_clears_the_count(self):
        dead = json.dumps({"ok": True, "action": "click", "screen_changed": False})
        live = json.dumps({"ok": True, "action": "click", "screen_changed": True})
        stalls = 0
        for _ in range(3):
            _, stalls = server._computer_progress(dead, stalls)
        _, stalls = server._computer_progress(live, stalls)
        self.assertEqual(stalls, 0)

    def test_changing_tools_does_not_launder_the_repetition(self):
        # The same key on the same screen, through the other door. Keying the
        # identity on the tool would let a run alternate its way out.
        self.assertEqual(
            server._computer_repeat_key({"action": "key", "text": "ctrl+a"}, "computer"),
            server._computer_repeat_key({"action": "key", "text": "ctrl+a"}, "browser"),
        )

    def test_looking_is_never_counted_as_a_duplicate_call(self):
        # Six snapshots were refused as duplicates in one run, which took away
        # the only move that gets a stuck run unstuck: looking again after the
        # page changed.
        self.assertTrue(server._is_a_look({"name": "browser", "arguments": {"action": "snapshot"}}))
        self.assertTrue(server._is_a_look({"name": "computer", "arguments": {"action": "screenshot"}}))
        self.assertFalse(server._is_a_look({"name": "browser", "arguments": {"action": "click", "ref": "e1"}}))
        # A batch is only a look if every step in it is.
        self.assertTrue(server._is_a_look(
            {"name": "computer", "arguments": {"actions": [{"action": "screenshot"}]}}))
        self.assertFalse(server._is_a_look(
            {"name": "computer", "arguments": {"actions": [{"action": "screenshot"}, {"action": "click"}]}}))

    def test_an_ordinary_tool_is_not_exempted(self):
        # read_file repeated with identical arguments really is a repeat, and
        # the guard that catches it must stay as it was.
        self.assertFalse(server._is_a_look({"name": "read_file", "arguments": {"path": "a.py"}}))

    def test_a_browser_action_moving_the_page_is_a_new_call(self):
        call = {"name": "browser", "arguments": {"action": "click", "ref": "e1"}}
        with patch.object(server.browser, "state_id", return_value=1):
            first = server._call_signature(call, None)
        with patch.object(server.browser, "state_id", return_value=2):
            second = server._call_signature(call, None)
        self.assertNotEqual(first, second)

    def test_a_browser_action_against_a_frozen_page_is_a_repeat(self):
        call = {"name": "browser", "arguments": {"action": "click", "ref": "e1"}}
        with patch.object(server.browser, "state_id", return_value=7):
            self.assertEqual(
                server._call_signature(call, None), server._call_signature(call, None)
            )

    def test_a_browser_look_is_a_look_too(self):
        for action in ("snapshot", "read", "status", "elements"):
            self.assertIsNone(
                server._computer_repeat_key({"action": action}, "browser"), action
            )

    def test_a_key_pressed_over_and_over_is_one_repeated_action(self):
        # The failure this exists for: Return in a document changes the screen
        # every time, so neither the unchanged-screen counter nor the
        # frame-aware signature sees anything wrong with pressing it forever.
        key = server._computer_repeat_key({"action": "key", "text": "Return"})
        self.assertEqual(key, server._computer_repeat_key({"action": "key", "text": "Return"}))
        self.assertNotEqual(key, server._computer_repeat_key({"action": "key", "text": "Tab"}))

    def test_looking_between_actions_does_not_break_the_run(self):
        # A model that screenshots between every keystroke is working normally,
        # and counting those would reset the run this is meant to catch.
        self.assertIsNone(server._computer_repeat_key({"action": "screenshot"}))
        self.assertIsNone(server._computer_repeat_key({"action": "wait", "seconds": 2}))

    def test_the_same_click_at_a_different_place_is_a_different_action(self):
        self.assertNotEqual(
            server._computer_repeat_key({"action": "click", "x": 10, "y": 10}),
            server._computer_repeat_key({"action": "click", "x": 10, "y": 40}),
        )

    def test_asking_for_repeats_is_not_the_same_as_repeating(self):
        # `count` is the sanctioned way to press a key several times, so it has
        # to be part of the identity or the guard would fire on one call.
        self.assertNotEqual(
            server._computer_repeat_key({"action": "key", "text": "Return"}),
            server._computer_repeat_key({"action": "key", "text": "Return", "count": 3}),
        )

    def test_looking_is_not_a_stalled_action(self):
        shot = json.dumps({"ok": True, "action": "screenshot", "screen_changed": True})
        _, stalls = server._computer_progress(shot, 3)
        self.assertEqual(stalls, 0)


@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class RunawayKeyTests(unittest.IsolatedAsyncioTestCase):
    """The wall a real run hit: a report typed into a Google Doc, formatting the
    model did not like, and then Return pressed forever to push it around.

    Neither earlier guard could see it. Return adds a line, so the screen
    changes on every press: the unchanged-screen counter resets each time, and
    the frame folded into the duplicate-call signature makes every press a new
    call. What identifies it is the repetition itself.
    """

    async def _run_pressing_return(self, presses, look_between=False, tool="computer"):
        performed = []
        # Every press adds a line, so the frame is new each time. Holding this
        # constant would let the duplicate-call guard catch it and prove
        # nothing: it is exactly what does not happen on a real desktop.
        frames = iter(range(1, 10_000))

        turns = []

        async def fake_stream_chat(*args, **kwargs):
            if len(performed) >= presses:
                yield {"message": {"content": "Done."}, "done": True, "done_reason": "stop"}
                return
            turns.append(1)
            looking = "screenshot" if tool == "computer" else "snapshot"
            args_out = (
                {"action": looking}
                if look_between and len(turns) % 2 == 0
                else {"action": "key", "text": "Return"}
            )
            yield {
                "message": {"tool_calls": [{
                    "function": {"name": tool, "arguments": args_out},
                }]},
                "done": True,
                "done_reason": "stop",
            }

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        def fake_run_tool(name, args, *rest, **kw):
            if args.get("action") in ("screenshot", "snapshot"):
                return json.dumps({"ok": True, "action": args["action"], "screen_changed": True})
            performed.append(args)
            # A keypress in a document works, and moves the page: the honest
            # result here is success plus a changed screen.
            return json.dumps({"ok": True, "action": "key", "screen_changed": True})

        chat = {
            "id": "runaway", "project_id": None, "computer_use": True,
            "messages": [{"role": "user", "content": "Tidy up the document."}],
        }
        meta = {"id": "seer", "keep_alive": "1m", "temperature": 0.2, "context": 32768}
        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.agent, "run_tool", side_effect=fake_run_tool),
            patch.object(server.computer, "pop_frame", return_value=None),
            patch.object(server.computer, "frame_id", side_effect=lambda: next(frames)),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3,
                "role_context": {"agentic": 32768},
                "shell_approval_mode": "never_ask",
            }),
        ):
            events = [e async for e in server._agent_loop(chat, meta, "seer", think=False)]
        return performed, events, chat

    async def test_the_key_stops_being_pressed_after_a_run_of_them(self):
        performed, events, chat = await self._run_pressing_return(50)
        self.assertLess(
            len(performed), 6,
            f"Return reached the desktop {len(performed)} times before anything stopped it",
        )
        corrections = [
            m for m in chat["messages"]
            if m.get("role") == "tool" and "same action on the screen" in (m.get("content") or "")
        ]
        self.assertTrue(corrections, "the model was never told it was repeating itself")
        self.assertIn("ctrl+z", corrections[0]["content"])

    async def test_a_model_that_ignores_the_correction_ends_the_run(self):
        # Refusing one press in four only slows a run-away down, and the generic
        # stall counter cannot end it either: a screenshot between two presses
        # is a successful call and resets it.
        _, events, chat = await self._run_pressing_return(50)
        self.assertTrue(chat.get("interrupted"))
        final = [e for e in events if e.get("type") == "message"][-1]["message"]
        self.assertIn("stopped rather than carry on", final["content"])
        self.assertIn("ctrl+z", final["content"])

    async def test_the_same_run_away_through_the_browser_is_also_stopped(self):
        # It happened: ctrl+a through the browser, fourteen times, while the
        # desktop tool was the only one the guard knew about. What the generic
        # duplicate-call guard gave it back was advice about writing files, so
        # the test looks for this guard's own correction, not merely a stop.
        performed, _, chat = await self._run_pressing_return(50, tool="browser")
        self.assertLess(len(performed), 6)
        self.assertTrue(chat.get("interrupted"))
        corrections = [
            m for m in chat["messages"]
            if m.get("role") == "tool" and "same action on the screen" in (m.get("content") or "")
        ]
        self.assertTrue(corrections, "the browser run-away got only the generic guard")

    async def test_looking_between_presses_does_not_launder_the_repetition(self):
        # The shape a real run takes: press, screenshot, press, screenshot. Each
        # screenshot is a successful tool call, so it resets every counter the
        # loop had before this guard.
        performed, _, chat = await self._run_pressing_return(50, look_between=True)
        self.assertLess(len(performed), 6)
        self.assertTrue(chat.get("interrupted"))

    async def test_a_run_can_keep_looking_without_being_told_to_stop(self):
        """Six snapshots in one run came back refused as duplicates.

        The duplicate-call guard counts identical arguments and assumes an
        identical result. For an observation that is false, since the point
        of a second snapshot is that a menu opened since the first, and
        refusing them took away the one move that gets a stuck run unstuck.
        """
        looks = []
        turns = []

        async def fake_stream_chat(*args, **kwargs):
            if len(looks) >= 6:
                yield {"message": {"content": "Done."}, "done": True, "done_reason": "stop"}
                return
            turns.append(1)
            # Look, act, look, act: the ordinary rhythm of driving a page. The
            # snapshots are identical calls and must not be refused as repeats,
            # which is a different question from whether looking is all it does.
            args_out = ({"action": "snapshot"} if len(turns) % 2
                        else {"action": "click", "ref": "e1"})
            yield {
                "message": {"tool_calls": [{
                    "function": {"name": "browser", "arguments": args_out},
                }]},
                "done": True, "done_reason": "stop",
            }

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        def fake_run_tool(name, args, *rest, **kw):
            if args.get("action") == "snapshot":
                looks.append(args)
                return json.dumps({"ok": True, "elements": [{"ref": "e1", "name": "Bold"}]})
            # Only an act moves the page, which is what the real browser
            # reports. Advancing on a look as well would give every snapshot a
            # unique signature and leave this test unable to see the thing it
            # is for.
            world[0] += 1
            return json.dumps({"ok": True, "action": "click", "ref": "e1"})

        world = [0]
        chat = {
            "id": "looking", "project_id": None, "computer_use": True,
            "messages": [{"role": "user", "content": "Format the document."}],
        }
        meta = {"id": "seer", "keep_alive": "1m", "temperature": 0.2, "context": 32768}
        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.agent, "run_tool", side_effect=fake_run_tool),
            patch.object(server.computer, "pop_frame", return_value=None),
            patch.object(server.browser, "state_id", side_effect=lambda: world[0]),
            patch.object(server.st, "save_chat", side_effect=lambda v: v),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3,
                "role_context": {"agentic": 32768},
                "shell_approval_mode": "never_ask",
            }),
        ):
            [e async for e in server._agent_loop(chat, meta, "seer", think=False)]

        self.assertEqual(len(looks), 6, "a look was refused, so the run went blind")
        refused = [
            m for m in chat["messages"]
            if m.get("role") == "tool" and "already called" in (m.get("content") or "")
        ]
        self.assertEqual(refused, [], "a look was refused as a duplicate call")

    async def _run_only_looking(self, looks_wanted):
        """A model that only ever asks for another screenshot."""
        looks = []

        async def fake_stream_chat(*args, **kwargs):
            if len(looks) >= looks_wanted:
                yield {"message": {"content": "Done."}, "done": True, "done_reason": "stop"}
                return
            yield {
                "message": {"tool_calls": [{
                    "function": {"name": "computer", "arguments": {"action": "screenshot"}},
                }]},
                "done": True, "done_reason": "stop",
            }

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        def fake_run_tool(name, args, *rest, **kw):
            looks.append(args)
            return json.dumps({"ok": True, "action": "screenshot", "screen_changed": True})

        chat = {
            "id": "staring", "project_id": None, "computer_use": True,
            "messages": [{"role": "user", "content": "Write the essay."}],
        }
        meta = {"id": "seer", "keep_alive": "1m", "temperature": 0.2, "context": 32768}
        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.agent, "run_tool", side_effect=fake_run_tool),
            patch.object(server.computer, "pop_frame", return_value=None),
            patch.object(server.st, "save_chat", side_effect=lambda v: v),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3,
                "role_context": {"agentic": 32768},
                "shell_approval_mode": "never_ask",
            }),
        ):
            events = [e async for e in server._agent_loop(chat, meta, "seer", think=False)]
        return looks, events, chat

    async def test_a_run_that_only_looks_is_stopped(self):
        """A run took 265 screenshots in a row and never acted on one.

        Exempting observations from the duplicate-call guard was right, since
        a second snapshot after a menu opens is a new call, but it was
        the only brake there was, and looking is how you decide what to do,
        not a thing to do.
        """
        looks, _, chat = await self._run_only_looking(400)
        self.assertLess(len(looks), 12, f"took {len(looks)} looks before anything stopped it")
        self.assertTrue(chat.get("interrupted"))
        refusals = [
            m for m in chat["messages"]
            if m.get("role") == "tool" and "looks in a row" in (m.get("content") or "")
        ]
        self.assertTrue(refusals)
        self.assertIn("stop and write the debrief", refusals[0]["content"])

    async def test_looking_a_few_times_to_decide_is_fine(self):
        # Screenshot, zoom into a region, zoom into another: that is reading the
        # screen, and it must not be mistaken for staring at it.
        looks, _, _ = await self._run_only_looking(server.LOOK_LIMIT)
        self.assertEqual(len(looks), server.LOOK_LIMIT)

    async def test_doing_something_earns_the_looks_back(self):
        # The count is of looks with nothing done in between, so a run that
        # acts between them is working normally however long it goes on.
        done = []

        async def fake_stream_chat(*args, **kwargs):
            if len(done) >= 2 * server.LOOK_LIMIT + 4:
                yield {"message": {"content": "Done."}, "done": True, "done_reason": "stop"}
                return
            step = len(done) % 2 == 0
            yield {
                "message": {"tool_calls": [{
                    "function": {"name": "computer", "arguments": (
                        {"action": "screenshot"} if step
                        else {"action": "type", "text": f"paragraph {len(done)}"}
                    )},
                }]},
                "done": True, "done_reason": "stop",
            }

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        def fake_run_tool(name, args, *rest, **kw):
            done.append(args)
            return json.dumps({"ok": True, "action": args["action"], "screen_changed": True})

        chat = {
            "id": "working", "project_id": None, "computer_use": True,
            "messages": [{"role": "user", "content": "Write the essay."}],
        }
        meta = {"id": "seer", "keep_alive": "1m", "temperature": 0.2, "context": 32768}
        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.agent, "run_tool", side_effect=fake_run_tool),
            patch.object(server.computer, "pop_frame", return_value=None),
            patch.object(server.st, "save_chat", side_effect=lambda v: v),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3,
                "role_context": {"agentic": 32768},
                "shell_approval_mode": "never_ask",
            }),
        ):
            [e async for e in server._agent_loop(chat, meta, "seer", think=False)]
        self.assertEqual(
            len(done), 2 * server.LOOK_LIMIT + 4,
            "a run that acts between looks was interrupted",
        )

    async def test_a_few_presses_in_a_row_are_left_alone(self):
        # Three Returns is a blank line between paragraphs, not a run-away.
        performed, _, _ = await self._run_pressing_return(3)
        self.assertEqual(len(performed), 3)


@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class ModelRoutingTests(unittest.TestCase):
    """Computer use belongs to any model that can see, not to one of them."""

    REGISTRY = {
        "seer:27b": {"id": "seer:27b", "vision": True, "tools": True},
        "other-seer:24b": {"id": "other-seer:24b", "vision": True, "tools": True},
        "blind:30b": {"id": "blind:30b", "vision": False, "tools": True},
    }

    def _patched(self):
        return patch.object(
            server, "_registry", return_value=({}, dict(self.REGISTRY))
        )

    def test_a_chat_keeps_its_own_vision_model(self):
        chat = {"mode": "agentic", "computer_use": True, "model_id": "other-seer:24b"}
        with self._patched():
            self.assertEqual(server._model_key(chat), "other-seer:24b")

    def test_a_blind_selection_is_routed_to_a_model_that_can_see(self):
        chat = {"mode": "agentic", "computer_use": True, "model_id": "blind:30b"}
        with self._patched():
            self.assertIn(server._model_key(chat), ("seer:27b", "other-seer:24b"))

    def test_a_blind_per_request_override_does_not_win_a_computer_use_chat(self):
        chat = {"mode": "agentic", "computer_use": True, "model_id": "seer:27b"}
        with self._patched():
            self.assertNotEqual(server._model_key(chat, model_key="blind:30b"), "blind:30b")

    def test_a_grounding_model_is_preferred_when_nothing_was_chosen(self):
        # Seeing a button and being able to say where it is are different
        # skills. Six clicks at the same wrong spot is what the second one
        # missing looks like, and it is not fixed by a bigger general model.
        registry = {
            "seer:27b": {"id": "seer:27b", "vision": True, "tools": True},
            "qwen3-vl:8b": {"id": "qwen3-vl:8b", "vision": True, "tools": True},
        }
        with patch.object(server, "_registry", return_value=({}, registry)):
            chat = {"mode": "agentic", "computer_use": True}
            self.assertEqual(server._model_key(chat), "qwen3-vl:8b")

    def test_the_chosen_model_still_wins_over_the_grounding_one(self):
        registry = {
            "seer:27b": {"id": "seer:27b", "vision": True, "tools": True},
            "qwen3-vl:8b": {"id": "qwen3-vl:8b", "vision": True, "tools": True},
        }
        with patch.object(server, "_registry", return_value=({}, registry)):
            chat = {"mode": "agentic", "computer_use": True, "model_id": "seer:27b"}
            self.assertEqual(server._model_key(chat), "seer:27b")

    def test_a_vision_model_that_cannot_call_tools_is_not_an_option(self):
        # qwen2.5vl is the live example: Ollama reports it with vision and
        # without tools, so it can see a screen and never act on one.
        registry = {
            "qwen2.5vl:7b": {"id": "qwen2.5vl:7b", "vision": True, "tools": False},
            "seer:27b": {"id": "seer:27b", "vision": True, "tools": True},
        }
        with patch.object(server, "_registry", return_value=({}, registry)):
            chat = {"mode": "agentic", "computer_use": True}
            self.assertEqual(server._model_key(chat), "seer:27b")

    def test_without_computer_use_the_selection_is_untouched(self):
        chat = {"mode": "agentic", "model_id": "blind:30b"}
        with self._patched():
            self.assertEqual(server._model_key(chat), "blind:30b")


if __name__ == "__main__":
    unittest.main()
