# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Navigation correctness across edits, ignores, ranking and model changes."""
import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import code_structure
import repo_index as ri
import symbols
from ollama_client import estimate_tokens


class FakeEmbedder:
    model = "fake@digest-one:v1"

    def __init__(self):
        self.calls = 0

    def identity(self):
        return self.model

    def embed(self, texts, query=False):
        self.calls += 1
        # A query with no literal overlap can still find the billing handler.
        return [[1.0, 0.0] if query or "invoice" in text else [0.0, 1.0] for text in texts]


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "project"
        self.root.mkdir()
        self.cache = base / "cache"
        self.index = ri.ProjectIndex(self.root, self.cache)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def test_incremental_cache_survives_new_instances_and_external_edits(self):
        path = self.write("billing.py", "def authenticate_token(token):\n    return token\n")
        with patch.object(ri.code_structure, "facts", wraps=code_structure.facts) as parse:
            self.assertEqual(self.index.refresh()["changed"], 1)
            other = ri.ProjectIndex(self.root, self.cache)
            self.assertEqual(other.refresh()["changed"], 0)
            self.assertEqual(parse.call_count, 1)
            previous = path.stat()
            path.write_text("def authenticate_token(token):\n    return False\n", encoding="utf-8")
            os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            got = other.search("authenticate_token", "hybrid")
            self.assertIn("return False", got["hits"][0]["text"])
            self.assertEqual(parse.call_count, 2)

    def test_deleted_files_and_their_vectors_disappear(self):
        path = self.write("invoice.py", "def invoice():\n    return 1\n")
        self.index.refresh()
        self.assertEqual(self.index.embedding_batch(FakeEmbedder()), 1)
        path.unlink()
        self.assertEqual(self.index.refresh()["removed"], 1)
        with self.index.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM vectors").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM search").fetchone()[0], 0)
        self.assertEqual(self.index.search("invoice")["hits"], [])

    def test_gitignore_nested_rules_custom_exclusions_and_secrets(self):
        self.write(".gitignore", "private/\n*.tmp.py\n")
        self.write(".olithignore", "local.py\n")
        self.write("private/secret.py", "SECRET = True")
        self.write("private.tmp.py", "SECRET = True")
        self.write("local.py", "SECRET = True")
        self.write(".env.py", "SECRET = True")
        self.write("node_modules/noise.js", "const SECRET = true;")
        self.write("src/.gitignore", "skip.py\n")
        self.write("src/skip.py", "SECRET = True")
        self.write("src/keep.py", "def visible(): pass")
        self.assertEqual(self.index.refresh()["files"], 1)
        self.assertEqual(self.index.search("SECRET")["hits"], [])

    def test_git_inventory_keeps_tracked_files_and_finds_untracked_files(self):
        self.write("tracked.py", "def tracked(): pass")
        try:
            subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(self.root), "add", "tracked.py"], check=True, capture_output=True)
        except FileNotFoundError:
            self.skipTest("Git is unavailable")
        self.write(".gitignore", "tracked.py\nignored.py\n")
        self.write("ignored.py", "def ignored(): pass")
        self.write("new.py", "def untracked(): pass")
        self.assertEqual(self.index.refresh()["files"], 2)

    def test_symlinks_and_oversized_files_are_not_indexed(self):
        outside = self.root.parent / "outside.py"
        outside.write_text("def secret(): pass")
        try:
            (self.root / "link.py").symlink_to(outside)
        except OSError:
            self.skipTest("Symlinks are unavailable")
        self.write("large.py", "x" * (ri.MAX_BYTES + 1))
        self.write("binary.py", "a\x00b")
        self.assertEqual(self.index.refresh()["files"], 0)

    def test_definition_and_reference_navigation_has_real_lines(self):
        self.write("src/billing.py", "def authenticate_token(token):\n    return token\n")
        self.write("src/api.py", "from .billing import authenticate_token\nresult = authenticate_token('x')\n")
        got = self.index.search("authenticate_token", "symbols")
        self.assertEqual([(h["file"], h["line"]) for h in got["hits"]], [("src/billing.py", 1)])
        got = self.index.search("authenticate_token", "references")
        self.assertTrue(any(h["file"] == "src/api.py" and h["line"] == 2 for h in got["hits"]))
        self.assertIn("not compiler-resolved", got["hint"])

    def test_dependency_rank_and_task_relevance_both_influence_map(self):
        self.write("core.py", "def foundation(): pass\n")
        for n in range(8):
            self.write(f"client{n}.py", "import core\ncore.foundation()\n")
        self.write("obscure.py", "def authenticate_payment_token(): pass\n")
        self.index.refresh()
        with self.index.db() as db:
            records = self.index._records(db)
        graph = self.index._graph(records)
        ranks = ri.pagerank(graph)
        self.assertGreater(ranks["core.py"], ranks["client0.py"])
        repo_map = self.index.repo_map("authenticate payment token", 400)
        self.assertIn("obscure.py", repo_map)
        self.assertLess(repo_map.index("obscure.py"), repo_map.index("core.py"))
        self.assertLessEqual(estimate_tokens(repo_map), 400)
        self.assertEqual(self.index.repo_map("", 50), "")

    def test_relative_python_and_javascript_imports_form_graph_edges(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/auth.py", "def verify(): pass")
        self.write("pkg/api.py", "from .auth import verify\nverify()")
        self.write("ui/auth.ts", "export const verify = () => true;")
        self.write("ui/api.ts", "import {verify} from './auth';\nverify();")
        self.index.refresh()
        with self.index.db() as db:
            graph = self.index._graph(self.index._records(db))
        self.assertIn("pkg/auth.py", graph["pkg/api.py"])
        self.assertIn("ui/auth.ts", graph["ui/api.ts"])

    def test_graph_does_not_treat_builtins_or_cross_language_names_as_dependencies(self):
        self.write("api.py", "kind = type(1)\ndef authenticate(): pass")
        self.write("desktop.py", "def type(text): pass")
        self.write("ui.js", "authenticate();")
        self.index.refresh()
        with self.index.db() as db:
            graph = self.index._graph(self.index._records(db))
        self.assertNotIn("desktop.py", graph["api.py"])
        self.assertNotIn("api.py", graph["ui.js"])

    def test_lexical_search_splits_camelcase_and_respects_scope(self):
        self.write("a/payment.ts", "export function authenticatePaymentToken() { return true; }")
        self.write("b/payment.ts", "export function authenticatePaymentToken() { return false; }")
        got = self.index.search("payment token", subpath="a", glob="**/*.ts")
        self.assertTrue(got["hits"])
        self.assertTrue(all(h["file"].startswith("a/") for h in got["hits"]))

    def test_scope_is_applied_before_the_candidate_limit(self):
        for n in range(305):
            self.write(f"noise/f{n:03}.py", "def needle():\n    return 1\n")
        self.write("target/match.py", "def needle():\n    return 1\n")
        got = self.index.search("needle", subpath="target")
        self.assertEqual(got["hits"][0]["file"], "target/match.py")

    def test_partial_inventory_cannot_return_a_newly_ignored_cached_file(self):
        self.write("a.py", "def first(): pass")
        self.write("b.py", "def second(): pass")
        self.write("z.py", "def private_data(): pass")
        self.index.refresh()
        self.write(".olithignore", "z.py\n")
        with patch.object(ri, "MAX_FILES", 1):
            got = self.index.search("private_data")
        self.assertTrue(got["index"]["truncated"])
        self.assertEqual(got["hits"], [])

    def test_semantic_retrieval_and_model_change_fallback(self):
        self.write("invoice.py", "def invoice():\n    return 1\n")
        self.write("weather.py", "def temperature():\n    return 2\n")
        self.index.refresh()
        embedder = FakeEmbedder()
        self.assertEqual(self.index.embedding_batch(embedder), 2)
        self.assertEqual(self.index.embedding_batch(embedder), 0)
        self.assertEqual(embedder.calls, 1)
        result = self.index.search("settle outstanding charges", embedder=embedder)
        self.assertTrue(result["semantic"]["available"])
        self.assertEqual(result["hits"][0]["file"], "invoice.py")
        embedder.model = "fake@digest-two:v1"
        result = self.index.search("invoice", embedder=embedder)
        self.assertFalse(result["semantic"]["available"])
        self.assertEqual(result["hits"][0]["file"], "invoice.py")
        self.assertEqual(self.index.embedding_batch(embedder), 2)

    def test_missing_embedding_service_keeps_lexical_search_working(self):
        self.write("auth.py", "def authenticate_token(): pass")
        embedder = FakeEmbedder()
        embedder.identity = lambda: (_ for _ in ()).throw(ConnectionError("offline"))
        result = self.index.search("authenticate_token", embedder=embedder)
        self.assertTrue(result["hits"])
        self.assertIn("offline", result["semantic"]["reason"])

    def test_results_do_not_return_source_changed_after_manifest_refresh(self):
        path = self.write("auth.py", "def authenticate_token(): pass")
        self.index.refresh()
        with patch.object(self.index, "refresh", return_value={"chunks": 1}):
            path.write_text("def different(): pass")
            self.assertEqual(self.index.search("authenticate_token")["hits"], [])

    def test_snippets_fit_the_tool_budget(self):
        for n in range(12):
            self.write(f"f{n}.py", "def payment():\n" + "    # payment token " + "x" * 1500 + "\n")
        result = self.index.search("payment", limit=20)
        self.assertLess(len(json.dumps(result)), 8000)


class ParserTests(unittest.TestCase):
    def test_js_boundaries_ignore_string_template_and_regex_braces(self):
        text = 'export function first() {\n const a = "}";\n const b = /[{}]/;\n const c = `hello ${a}`;\n return c;\n}\nfunction second() {}\n'
        if not code_structure.parser_for("x.js"):
            self.skipTest("Tree-sitter grammars unavailable")
        entries = symbols.outline(text, "x.js")
        self.assertEqual(entries[0]["end_line"], 6)
        self.assertTrue(entries[0]["exact"])
        self.assertNotIn("second", symbols.find(text, "x.js", "first")["source"])

    def test_typescript_tsx_go_rust_java_c_and_cpp(self):
        fixtures = {
            "x.ts": ("export interface Token { value: string }\nexport function verify(t: Token): boolean { return true; }", "verify"),
            "x.tsx": ("export const Panel = () => <div>{'}'}</div>;", "Panel"),
            "x.go": ("package main\nfunc Verify() bool { return true }", "Verify"),
            "x.rs": ("pub fn verify() -> bool { true }", "verify"),
            "X.java": ("class X { public boolean verify() { return true; } }", "X.verify"),
            "x.c": ("int verify(void) { return 1; }", "verify"),
            "x.cpp": ("int verify() { return 1; }", "verify"),
        }
        for path, (text, name) in fixtures.items():
            with self.subTest(path=path):
                if not code_structure.parser_for(path):
                    self.skipTest("Tree-sitter grammars unavailable")
                self.assertTrue(any(e["name"] == name and e["exact"] for e in symbols.outline(text, path)))

    def test_syntax_errors_are_marked_and_fallback_remains_honest(self):
        info = code_structure.facts("function broken( {", "x.js")
        self.assertTrue(info["parse_errors"])
        with patch.object(code_structure, "parser_for", return_value=None):
            symbols._outline_cached.cache_clear()
            got = symbols.outline("function fallback() { return '}'; }", "fallback.js")
            self.assertFalse(got[0]["exact"])
        symbols._outline_cached.cache_clear()


class EmbedProtocolTests(unittest.TestCase):
    def test_cpu_batch_request_prefixes_normalization_and_digest(self):
        import httpx
        embedder = ri.OllamaEmbedder({"embedding_model": "nomic-embed-text", "embedding_device": "cpu"})
        tags = httpx.Response(200, json={"models": [{"name": "nomic-embed-text:latest", "digest": "abc"}]}, request=httpx.Request("GET", "http://localhost"))
        response = httpx.Response(200, json={"embeddings": [[3, 4]]}, request=httpx.Request("POST", "http://localhost"))
        with patch.object(httpx, "get", return_value=tags), patch.object(httpx, "post", return_value=response) as post:
            self.assertEqual(embedder.identity(), "nomic-embed-text@abc:v1")
            self.assertEqual(embedder.embed(["verify"], query=True), [[0.6, 0.8]])
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["input"], ["search_query: verify"])
        self.assertEqual(body["options"], {"num_gpu": 0})
        self.assertFalse(body["truncate"])
        self.assertEqual(body["keep_alive"], 0)

    def test_invalid_vectors_are_rejected(self):
        import httpx
        embedder = ri.OllamaEmbedder({"embedding_model": "fake"})
        for vectors in ([[0, 0]], [["nan", 1]], [], [[1], [1]]):
            response = httpx.Response(200, json={"embeddings": vectors}, request=httpx.Request("POST", "http://localhost"))
            with patch.object(embedder, "identity", return_value="fake@one:v1"), patch.object(httpx, "post", return_value=response):
                with self.assertRaises(ValueError):
                    embedder.embed(["test"])


if __name__ == "__main__":
    unittest.main()
