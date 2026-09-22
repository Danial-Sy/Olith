# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Per-model policy: tiers, the rejected dials, and tool-name aliases.

Every size-based dial was implemented, benchmarked against real models, and
turned off because it measured worse. What these tests protect is that
outcome: the mechanisms still work when the ablation asks for them, and no
tier turns one on. A future change has to beat a measurement, not an argument.

See `scripts/bench_tool_choice.py` for the harness and ARCHITECTURE.md for the
summary.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent
import policy

B = 1_000_000_000

# Explicit dial settings, used only here and by the benchmark. Nothing ships
# with any of these on.
ORDERING = {"tool_budget": None, "order_tools": True, "lean_schemas": False}
LEANING = {"tool_budget": None, "order_tools": False, "lean_schemas": True}
BUDGETED = {"tool_budget": 9, "order_tools": False, "lean_schemas": False}


def _profile(params_b):
    return {"params": int(params_b * B)}


def _names(defs):
    return [d["function"]["name"] for d in defs]


class TierTests(unittest.TestCase):
    def test_each_band_lands_in_its_own_tier(self):
        for params, expected in ((0.5, "tiny"), (3, "small"), (7.6, "small"),
                                 (14.8, "mid"), (27, "full"), (30, "full")):
            self.assertEqual(policy.tier_for(_profile(params)), expected, f"{params}B")

    def test_the_boundaries_belong_to_the_larger_tier(self):
        self.assertEqual(policy.tier_for(_profile(2)), "small")
        self.assertEqual(policy.tier_for(_profile(8)), "mid")
        self.assertEqual(policy.tier_for(_profile(20)), "full")

    def test_a_model_we_cannot_size_keeps_the_whole_surface(self):
        for unknown in ({}, None, {"params": 0}, {"params": None}):
            self.assertEqual(policy.tier_for(unknown), "full")

    def test_the_policy_table_cannot_be_mutated_through_a_caller(self):
        got = policy.policy_for(_profile(3))
        got["tool_budget"] = 99
        self.assertIsNone(policy.policy_for(_profile(3))["tool_budget"])

    def test_the_tier_is_reported_so_the_ui_can_say_which(self):
        self.assertEqual(policy.policy_for(_profile(3))["tier"], "small")


class NothingIsEnabledTests(unittest.TestCase):
    """The measured result, pinned so it cannot be undone by a flag flip."""

    def test_no_tier_enables_any_dial(self):
        for tier, pol in policy.POLICIES.items():
            self.assertIsNone(pol["tool_budget"], f"{tier} re-enabled the budget")
            self.assertFalse(pol["order_tools"], f"{tier} re-enabled ordering")
            self.assertFalse(pol["lean_schemas"], f"{tier} re-enabled leaning")

    def test_every_size_receives_the_same_schemas(self):
        # Including the sizes the dials used to change. A 0.5B and a 27B get
        # identical tools, because nothing size-based survived measurement.
        # Computer use is the one thing that does change the set, and it is a
        # property of the chat rather than of the model, so it is on here to
        # keep the comparison against the complete list.
        chat = {"delegate_available": True, "computer_use": True}
        expected = json.dumps(agent.TOOL_DEFS, ensure_ascii=False)
        for params in (0.5, 3, 7.6, 14.8, 27, 30):
            got = agent.tool_defs_for(chat, 65536, _profile(params))
            self.assertEqual(json.dumps(got, ensure_ascii=False), expected, f"{params}B")

    def test_an_unprofiled_run_is_identical_too(self):
        chat = {"delegate_available": True, "computer_use": True}
        self.assertEqual(agent.tool_defs_for(chat, 65536),
                         agent.tool_defs_for(chat, 65536, None))

    def test_prompt_overhead_is_the_same_whatever_the_model(self):
        from ollama_client import estimate_tokens
        chat = {"delegate_available": True}

        def overhead(profile):
            defs = agent.tool_defs_for(chat, 65536, profile)
            return agent.schema_tokens(defs) + estimate_tokens(agent.tools_system_addon(defs))

        self.assertEqual(overhead(_profile(0.5)), overhead(_profile(27)))
        self.assertEqual(overhead(None), overhead(_profile(27)))

    def test_trimming_for_a_small_window_still_happens(self):
        # Dropping schemas that do not fit the context is a different question
        # from dropping them by model size, and it was never in doubt.
        chat = {"delegate_available": True}
        self.assertLess(len(agent.tool_defs_for(chat, 8192, _profile(27))),
                        len(agent.TOOL_DEFS))


class EdgeOrderTests(unittest.TestCase):
    def test_priority_runs_outward_in(self):
        self.assertEqual(policy.edge_order(["a", "b", "c", "d", "e"]),
                         ["a", "c", "e", "d", "b"])

    def test_the_least_important_item_lands_in_the_middle(self):
        out = policy.edge_order(list(range(9)))
        self.assertEqual(out[len(out) // 2], 8)

    def test_it_reorders_rather_than_drops(self):
        for size in range(0, 15):
            items = list(range(size))
            self.assertCountEqual(policy.edge_order(items), items, f"n={size}")


class MechanismsStillWorkTests(unittest.TestCase):
    """Rejected, not deleted. The ablation has to be able to ask again."""

    def test_ordering_puts_the_most_important_tools_at_the_ends(self):
        got = _names(agent.apply_policy(agent.TOOL_DEFS, ORDERING))
        self.assertEqual(got[0], "read_file")
        self.assertEqual(got[-1], "edit_file")

    def test_ordering_moves_the_critical_tools_out_of_the_middle(self):
        before = _names(agent.TOOL_DEFS)
        after = _names(agent.apply_policy(agent.TOOL_DEFS, ORDERING))

        def dist(names, tool):
            return abs(names.index(tool) - (len(names) - 1) / 2) / ((len(names) - 1) / 2)

        for tool in ("edit_file", "search_files", "run_shell"):
            self.assertGreater(dist(after, tool), dist(before, tool), tool)

    def test_a_budget_keeps_the_highest_priority_tools(self):
        got = agent.apply_policy(agent.TOOL_DEFS, BUDGETED)
        self.assertEqual(len(got), 9)
        for essential in agent.ESSENTIAL_TOOLS:
            self.assertIn(essential, _names(got), essential)

    def test_a_budget_never_drops_below_the_essential_set(self):
        got = agent.apply_policy(agent.TOOL_DEFS, {**BUDGETED, "tool_budget": 2})
        self.assertCountEqual(_names(got), agent.ESSENTIAL_TOOLS)

    def test_a_budget_never_invents_a_tool_the_run_did_not_have(self):
        subset = [t for t in agent.TOOL_DEFS
                  if t["function"]["name"] in agent.SUBAGENT_DEFAULT_TOOLS]
        got = agent.apply_policy(subset, BUDGETED)
        self.assertTrue(set(_names(got)) <= set(agent.SUBAGENT_DEFAULT_TOOLS))

    def test_leaning_cuts_descriptions_to_one_sentence(self):
        lean = {d["function"]["name"]: d["function"].get("description", "")
                for d in agent.apply_policy(agent.TOOL_DEFS, LEANING)}
        full = {d["function"]["name"]: d["function"].get("description", "")
                for d in agent.TOOL_DEFS}
        self.assertLess(len(lean["todo_write"]), len(full["todo_write"]) / 2)

    def test_leaning_reaches_nested_parameter_prose(self):
        lean = next(d for d in agent.apply_policy(agent.TOOL_DEFS, LEANING)
                    if d["function"]["name"] == "todo_write")
        merge = lean["function"]["parameters"]["properties"]["merge"]["description"]
        self.assertNotIn("If false", merge)

    def test_leaning_never_changes_what_a_call_must_satisfy(self):
        def structure(node):
            if isinstance(node, dict):
                return {k: structure(v) for k, v in sorted(node.items())
                        if k != "description"}
            if isinstance(node, list):
                return [structure(v) for v in node]
            return node

        lean = {d["function"]["name"]: d["function"]["parameters"]
                for d in agent.apply_policy(agent.TOOL_DEFS, LEANING)}
        full = {d["function"]["name"]: d["function"]["parameters"] for d in agent.TOOL_DEFS}
        for name, params in lean.items():
            self.assertEqual(structure(params), structure(full[name]), name)

    def test_no_mechanism_mutates_the_shared_definitions(self):
        before = json.dumps(agent.TOOL_DEFS, ensure_ascii=False)
        for dials in (ORDERING, LEANING, BUDGETED):
            agent.apply_policy(agent.TOOL_DEFS, dials)
        self.assertEqual(json.dumps(agent.TOOL_DEFS, ensure_ascii=False), before)

    def test_an_abbreviation_does_not_end_a_sentence(self):
        self.assertEqual(agent._first_sentence("Pick one, e.g. alpha or beta. Then stop."),
                         "Pick one, e.g. alpha or beta.")


class AliasTests(unittest.TestCase):
    """The one thing that shipped, because it cannot regress anything.

    A model emitting a real tool name never reaches the alias table, so a
    strong model's behaviour is unchanged by construction.
    """

    def test_the_names_models_actually_reach_for_resolve(self):
        for alias, real in (("execute_command", "run_shell"), ("bash", "run_shell"),
                            ("str_replace", "edit_file"), ("grep", "search_files"),
                            ("ls", "list_dir"), ("create_file", "write_file")):
            self.assertEqual(agent.canonical_tool_name(alias), real, alias)

    def test_a_real_tool_name_always_wins(self):
        for name in agent.ESSENTIAL_TOOLS:
            self.assertEqual(agent.canonical_tool_name(name), name)

    def test_no_alias_shadows_a_registered_tool(self):
        real = {t["function"]["name"] for t in agent.TOOL_DEFS}
        self.assertEqual(set(agent.TOOL_ALIASES) & real, set())

    def test_every_alias_points_at_a_tool_that_exists(self):
        real = {t["function"]["name"] for t in agent.TOOL_DEFS}
        for alias, target in agent.TOOL_ALIASES.items():
            self.assertIn(target, real, alias)

    def test_an_unknown_name_is_left_alone(self):
        self.assertEqual(agent.canonical_tool_name("frobnicate"), "frobnicate")

    def test_a_native_call_under_an_alias_runs_the_real_tool(self):
        calls = agent.extract_tool_calls({
            "tool_calls": [{"function": {"name": "execute_command",
                                         "arguments": {"command": "ls"}}}]})
        self.assertEqual([c["name"] for c in calls], ["run_shell"])
        self.assertEqual(calls[0]["arguments"], {"command": "ls"})

    def test_an_aliased_call_written_into_the_reply_body_still_runs(self):
        calls = agent.extract_tool_calls({
            "content": '{"name": "str_replace", "arguments": {"path": "a.py"}}'})
        self.assertEqual([c["name"] for c in calls], ["edit_file"])

    def test_a_mangled_aliased_call_is_still_recognised_as_a_call(self):
        broken = '{"name": "execute_command", "arguments": ["command": "ls"]}'
        self.assertEqual(agent.looks_like_broken_tool_call(broken), "run_shell")

    def test_aliases_do_not_change_what_a_strong_model_sends(self):
        calls = agent.extract_tool_calls({
            "tool_calls": [{"function": {"name": "run_shell",
                                         "arguments": {"command": "ls"}}}]})
        self.assertEqual([c["name"] for c in calls], ["run_shell"])


class PriorityIntegrityTests(unittest.TestCase):
    def test_every_tool_is_ranked_exactly_once(self):
        names = [t["function"]["name"] for t in agent.TOOL_DEFS]
        self.assertCountEqual(agent.TOOL_PRIORITY, names)

    def test_the_essentials_outrank_everything_optional(self):
        ranks = {n: i for i, n in enumerate(agent.TOOL_PRIORITY)}
        worst_essential = max(ranks[n] for n in agent.ESSENTIAL_TOOLS)
        best_optional = min(ranks[n] for n in ranks if n not in agent.ESSENTIAL_TOOLS)
        self.assertLess(worst_essential, best_optional)


if __name__ == "__main__":
    unittest.main()
