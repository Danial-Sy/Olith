# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Symbol-level reads: outline and read-one-definition.

The point of the feature is that a model stops guessing line numbers, so the
tests that matter are the ones about boundaries being right and about the
result being smaller than the read it replaces.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent
import symbols

PY = '''"""Module docstring."""
import os


def alpha(a, b=2):
    """First."""
    return a + b


@decorated
@another
def beta():
    return 1


class Widget:
    """A widget."""

    def method_one(self):
        return "one"

    async def method_two(self, x,
                         y):
        return x + y


async def gamma():
    return None
'''

JS = """export function alpha(a, b) {
  return a + b;
}

const beta = (x) => {
  return x * 2;
};

export default class Widget {
  constructor() {}
}
"""


class _Tmp(unittest.TestCase):
    def write(self, name, text):
        d = tempfile.mkdtemp()
        p = Path(d) / name
        p.write_text(text, encoding="utf-8")
        return Path(d), p


class PythonOutlineTests(_Tmp):
    def test_it_finds_every_top_level_definition_and_method(self):
        got = {e["name"] for e in symbols.outline(PY, "m.py")}
        self.assertEqual(got, {"alpha", "beta", "Widget", "gamma",
                               "Widget.method_one", "Widget.method_two"})

    def test_python_boundaries_are_exact(self):
        for e in symbols.outline(PY, "m.py"):
            self.assertTrue(e["exact"], e["name"])

    def test_a_decorated_function_starts_at_its_first_decorator(self):
        # Copying a definition without its decorators produces wrong code.
        beta = next(e for e in symbols.outline(PY, "m.py") if e["name"] == "beta")
        self.assertEqual(PY.splitlines()[beta["line"] - 1], "@decorated")

    def test_a_wrapped_signature_is_reported_whole(self):
        got = symbols.find(PY, "m.py", "Widget.method_two")
        self.assertIn("x,", got["signature"])
        self.assertIn("y", got["signature"])

    def test_kinds_are_reported(self):
        kinds = {e["name"]: e["kind"] for e in symbols.outline(PY, "m.py")}
        self.assertEqual(kinds["Widget"], "class")
        self.assertEqual(kinds["alpha"], "function")


class PythonFindTests(_Tmp):
    def test_the_source_it_returns_is_the_whole_definition(self):
        got = symbols.find(PY, "m.py", "alpha")
        self.assertTrue(got["source"].startswith("def alpha(a, b=2):"))
        self.assertIn("return a + b", got["source"])
        self.assertNotIn("def beta", got["source"])

    def test_a_method_can_be_found_by_its_bare_name(self):
        # A model reading a traceback has the method name, not the class.
        got = symbols.find(PY, "m.py", "method_one")
        self.assertEqual(got["name"], "Widget.method_one")

    def test_a_dotted_name_also_works(self):
        self.assertEqual(symbols.find(PY, "m.py", "Widget.method_one")["name"],
                         "Widget.method_one")

    def test_an_async_definition_is_found(self):
        self.assertIsNotNone(symbols.find(PY, "m.py", "gamma"))

    def test_a_name_that_is_not_there_is_not_invented(self):
        self.assertIsNone(symbols.find(PY, "m.py", "nonexistent"))

    def test_a_file_that_does_not_parse_returns_nothing_rather_than_guessing(self):
        self.assertIsNone(symbols.outline("def broken(:\n  pass", "m.py"))

    def test_an_unsupported_language_returns_nothing(self):
        self.assertIsNone(symbols.outline("SELECT 1;", "q.sql"))


class JavaScriptTests(_Tmp):
    def test_it_finds_declarations_and_arrow_functions(self):
        got = {e["name"] for e in symbols.outline(JS, "m.js")}
        self.assertEqual(got, {"alpha", "beta", "Widget"})

    def test_javascript_results_are_flagged_inexact(self):
        # Brace counting cannot handle braces in strings or regex literals, and
        # the caller has to be able to say so.
        for e in symbols.outline(JS, "m.js"):
            self.assertFalse(e["exact"], e["name"])

    def test_boundaries_are_close_enough_to_be_useful(self):
        alpha = next(e for e in symbols.outline(JS, "m.js") if e["name"] == "alpha")
        self.assertEqual(alpha["line"], 1)
        self.assertEqual(alpha["end_line"], 3)


class ToolIntegrationTests(_Tmp):
    def _call(self, args):
        root, path = self.write("m.py", PY)
        return json.loads(agent.run_tool("read_file", {"path": "m.py", **args}, root))

    def test_reading_one_symbol_returns_it_with_real_line_numbers(self):
        got = self._call({"symbol": "alpha"})
        self.assertEqual(got["symbol"], "alpha")
        self.assertTrue(got["exact"])
        self.assertIn("5|def alpha(a, b=2):", got["content"])

    def test_a_missing_symbol_names_what_the_file_does_define(self):
        # A dead end that lists the alternatives becomes the next call.
        got = self._call({"symbol": "nope"})
        self.assertIn("error", got)
        self.assertIn("alpha", got["available"])
        self.assertIn("Widget.method_one", got["available"])

    def test_an_outline_is_one_line_per_definition(self):
        got = self._call({"outline": True})
        self.assertEqual(len(got["outline"].splitlines()), got["count"])
        self.assertIn("class", got["outline"])

    def test_an_unsupported_language_says_so_instead_of_failing(self):
        root, _ = self.write("q.sql", "SELECT 1;")
        got = json.loads(agent.run_tool("read_file", {"path": "q.sql", "outline": True}, root))
        self.assertFalse(got["symbols_supported"])
        self.assertIn("line range", got["hint"])

    def test_a_plain_read_is_completely_unaffected(self):
        # Every existing caller passes neither parameter and must not change.
        got = self._call({})
        self.assertIn("content", got)
        self.assertNotIn("symbol", got)
        self.assertEqual(got["start"], 1)

    def test_a_binary_file_is_still_refused_before_symbols_are_tried(self):
        root = Path(tempfile.mkdtemp())
        (root / "b.py").write_bytes(b"\x00\x01\x02binary")
        got = json.loads(agent.run_tool("read_file", {"path": "b.py", "symbol": "x"}, root))
        self.assertTrue(got["binary"])


class CostTests(_Tmp):
    """The feature exists to spend fewer tokens. Pin that it does."""

    def test_one_symbol_costs_less_than_reading_the_file(self):
        from ollama_client import estimate_tokens
        root = Path(__file__).resolve().parent.parent
        whole = json.loads(agent.run_tool("read_file", {"path": "server.py"}, root))
        one = json.loads(agent.run_tool("read_file",
                                        {"path": "server.py", "symbol": "_profile"}, root))
        self.assertLess(estimate_tokens(one["content"]),
                        estimate_tokens(whole["content"]) / 4)

    def test_an_outline_is_bounded_however_big_the_file(self):
        root = Path(__file__).resolve().parent.parent
        got = json.loads(agent.run_tool("read_file",
                                        {"path": "server.py", "outline": True}, root))
        self.assertLessEqual(len(got["outline"]), agent.READ_FILE_CONTENT_MAX_CHARS)
        self.assertTrue(got["truncated"])
        self.assertIn("more definitions", got["hint"])

    def test_a_long_symbol_is_capped_and_says_where_to_continue(self):
        root, _ = self.write("big.py", "def huge():\n" + "    x = 1\n" * 4000)
        got = json.loads(agent.run_tool("read_file",
                                        {"path": "big.py", "symbol": "huge"}, root))
        self.assertLessEqual(len(got["content"]), agent.READ_FILE_CONTENT_MAX_CHARS)
        self.assertTrue(got["truncated"])


if __name__ == "__main__":
    unittest.main()
