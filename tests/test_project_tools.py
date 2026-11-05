# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
import asyncio
import json
import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import project_commands as pc
import repo_index
import task_changes as tc
import language_servers as ls
import agent


class ProjectToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "project"
        self.root.mkdir()
        for patcher in (patch.object(repo_index, "INDEX_DIR", base / "indexes"),
                        patch.object(pc, "COMMAND_DIR", base / "commands"),
                        patch.object(tc, "CHANGE_DIR", base / "changes")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_successful_cpp_build_is_saved_for_its_source_and_manual_override_wins(self):
        self.write("game.cpp", "int main() { return 0; }")
        self.write("other.cpp", "int main() { return 0; }")
        self.assertTrue(pc.learn_build(self.root, "g++ -std=c++17 game.cpp -o game && echo BUILD-OK"))
        commands = pc.discover(self.root)
        self.assertEqual(commands[0]["source"], "learned")
        self.assertEqual(commands[0]["command"], "g++ -std=c++17 game.cpp -o game")
        self.assertEqual(len(pc.applicable(commands, ["game.cpp"])), 1)
        self.assertEqual(pc.applicable(commands, ["other.cpp"]), [])
        self.assertFalse(pc.learn_build(self.root, "g++ game.cpp | head -20; echo success"))
        self.assertFalse(pc.learn_build(self.root, "g++ game.cpp -o ../outside"))
        self.assertFalse(pc.learn_build(self.root, "g++ game.cpp -o../outside"))
        manual = pc.overrides(self.root, [{"kind": "build", "command": "make game", "cwd": "."}])
        self.assertEqual(manual[0]["command"], "make game")

    def test_discovery_saves_commands_and_manual_override_survives_config_changes(self):
        package = self.write("web/package.json", json.dumps({"scripts": {"test": "vitest", "build": "vite build", "typecheck": "tsc --noEmit"}}))
        found = pc.discover(self.root)
        self.assertEqual({c["kind"] for c in found}, {"test", "build", "typecheck"})
        self.assertIn("--run", next(c["command"] for c in found if c["kind"] == "test"))
        pc.overrides(self.root, [{"kind": "test", "command": "npm run test:focused", "cwd": "web"}])
        package.write_text(json.dumps({"scripts": {"test": "new-test"}}))
        found = pc.discover(self.root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["command"], "npm run test:focused")
        self.assertEqual(found[0]["source"], "manual")

    def test_generated_python_project_is_discovered_without_setup(self):
        self.write("tests/test_new.py", "import unittest\nclass TestNew(unittest.TestCase): pass\n")
        found = pc.discover(self.root)
        self.assertEqual(found[0]["kind"], "test")
        self.assertIn("unittest discover -s tests", found[0]["command"])
        self.assertTrue(pc._path(self.root).exists())

    def test_test_framework_is_found_in_combined_imports(self):
        self.write("test_game.py", "import json, subprocess, unittest\nclass Game(unittest.TestCase): pass\n")
        found = pc.discover(self.root)
        self.assertEqual(found[0]["kind"], "test")
        self.assertIn("unittest discover -s .", found[0]["command"])

    def test_monorepo_checks_follow_changed_paths_and_docs_do_not_trigger_builds(self):
        self.write("web/package.json", json.dumps({"scripts": {"build": "vite build"}}))
        self.write("api/Cargo.toml", "[package]\nname='api'\nversion='0.1.0'\n")
        found = pc.discover(self.root)
        self.assertEqual({c["cwd"] for c in pc.applicable(found, ["web/src/app.ts"])}, {"web"})
        self.assertEqual(pc.applicable(found, ["README.md"]), [])

    def test_override_cannot_escape_the_project(self):
        with self.assertRaises(ValueError):
            pc.overrides(self.root, [{"kind": "test", "command": "echo yes", "cwd": ".."}])

    def test_review_preserves_dirty_start_and_undoes_only_selected_block(self):
        path = self.write("app.py", "# already edited by user\na = 1\n\n\n\nb = 2\n")
        review = tc.ChangeSet(self.root).start()
        path.write_text("# already edited by user\na = 10\n\n\n\nb = 20\n")
        result = review.finish()
        hunks = result["files"][0]["hunks"]
        self.assertEqual(len(hunks), 2)
        result = review.undo([{"file": "app.py", "hunks": [hunks[0]["id"]]}])
        self.assertIn("a = 1\n", path.read_text())
        self.assertIn("b = 20\n", path.read_text())
        self.assertTrue(path.read_text().startswith("# already edited by user"))
        self.assertEqual(len(result["files"][0]["hunks"]), 1)

    def test_undo_refuses_later_edits_and_checks_all_files_before_writing(self):
        a = self.write("a.py", "x = 1\n")
        b = self.write("b.py", "x = 1\n")
        review = tc.ChangeSet(self.root).start()
        a.write_text("x = 2\n")
        b.write_text("x = 2\n")
        result = review.finish()
        b.write_text("# user typed this later\nx = 3\n")
        with self.assertRaises(ValueError):
            review.undo([{"file": f["file"], "hunks": [h["id"] for h in f["hunks"]]} for f in result["files"]])
        self.assertEqual(a.read_text(), "x = 2\n")
        self.assertIn("user typed", b.read_text())

    def test_created_deleted_and_empty_files_can_be_undone(self):
        deleted = self.write("old.py", "x = 1\n")
        review = tc.ChangeSet(self.root).start()
        deleted.unlink()
        self.write("new.py", "x = 2\n")
        self.write("empty.py", "")
        result = review.finish()
        self.assertEqual(len(result["files"]), 3)
        self.assertEqual(len(review.finish()["files"]), 3)
        review.undo([{"file": f["file"], "hunks": [h["id"] for h in f["hunks"]]} for f in result["files"]])
        self.assertEqual(deleted.read_text(), "x = 1\n")
        self.assertFalse((self.root / "new.py").exists())
        self.assertFalse((self.root / "empty.py").exists())

    @unittest.skipIf(os.name == "nt", "POSIX executable permissions")
    def test_undo_restores_deleted_script_permissions(self):
        script = self.write("run.sh", "#!/bin/sh\necho yes\n")
        script.chmod(0o755)
        review = tc.ChangeSet(self.root).start()
        script.unlink()
        result = review.finish()
        review.undo([{"file": "run.sh", "hunks": [h["id"] for h in result["files"][0]["hunks"]]}])
        self.assertEqual(script.stat().st_mode & 0o777, 0o755)

    def test_review_cannot_be_opened_from_another_project(self):
        self.write("a.py", "x=1")
        review = tc.ChangeSet(self.root).start()
        other = self.root.parent / "other"
        other.mkdir()
        with self.assertRaises(ValueError):
            tc.ChangeSet(other, review.ident).review()

    def test_snapshot_cap_does_not_label_preexisting_files_as_created(self):
        a = self.write("a.py", "#" + "a" * 99)
        b = self.write("b.py", "#" + "b" * 99)
        with patch.object(tc, "MAX_SNAPSHOT_BYTES", 120):
            review = tc.ChangeSet(self.root).start()
            a.unlink()
            result = review.finish()
        self.assertTrue(result["partial"])
        self.assertFalse(any(f["file"] == "b.py" for f in result["files"]))
        self.assertTrue(b.is_file())

    def test_language_server_navigation_distinguishes_bindings(self):
        a = self.write("a.py", "def calculate():\n    return 1\n")
        self.write("b.py", "def calculate():\n    return 2\n")
        use = self.write("use.py", "from a import calculate\nresult = calculate()\n")
        if not ls.server_command(self.root, "python"):
            self.skipTest("Python language server not installed")
        self.addCleanup(ls.close_all)
        result = ls.inspect_file(self.root, use, "definition", 2, 12)
        self.assertTrue(result["available"], result)
        self.assertEqual(result["hits"][0]["file"], "a.py")
        refs = ls.inspect_file(self.root, a, "references", 1, 5)
        self.assertTrue(any(h["file"] == "use.py" for h in refs["hits"]), refs)
        self.assertFalse(any(h["file"] == "b.py" for h in refs["hits"]))
        before = a.read_text()
        preview = ls.inspect_file(self.root, a, "rename", 1, 5, "compute")
        self.assertTrue(preview.get("preview_only"), preview)
        self.assertTrue(any(f["file"] == "a.py" for f in preview["files"]), preview)
        self.assertFalse(any(f["file"] == "b.py" for f in preview["files"]), preview)
        self.assertEqual(a.read_text(), before)

    def test_language_server_locations_cannot_escape_the_project(self):
        with self.assertRaises(ValueError):
            ls.local_path((self.root.parent / "private.py").as_uri(), self.root)
        with self.assertRaises(ValueError):
            ls.local_path("https://example.com/code.py", self.root)

    def test_typescript_server_returns_definitions_and_type_errors(self):
        self.write("tsconfig.json", '{"compilerOptions":{"strict":true},"include":["*.ts"]}')
        auth = self.write("auth.ts", "export function verify(value: string): boolean { return !!value; }\n")
        use = self.write("use.ts", "import {verify} from './auth';\nconst result: number = verify('token');\n")
        if not ls.server_command(self.root, "typescript"):
            self.skipTest("TypeScript language server not installed")
        self.addCleanup(ls.close_all)
        result = ls.inspect_file(self.root, use, "definition", 2, 25)
        self.assertTrue(result["available"], result)
        report = ls.inspect_file(self.root, use)
        self.assertTrue(any(hit["file"] == "auth.ts" for hit in result["hits"]), {"definition": result, "diagnostics": report})
        self.assertTrue(report["complete"], report)
        self.assertTrue(any(d.get("severity") == 1 for d in report["diagnostics"]), report)
        preview = ls.inspect_file(self.root, use, "rename", 2, 25, "verify_session")
        self.assertTrue(preview["preview_only"], preview)
        self.assertFalse(any(f["file"] == "auth.ts" for f in preview["files"]), preview)
        preview = ls.inspect_file(self.root, auth, "rename", 1, 18, "verify_session")
        self.assertTrue(any(f["file"] == "auth.ts" for f in preview["files"]), preview)
        self.assertIn("verify('token')", use.read_text())

    def test_python_language_server_reports_undefined_names(self):
        target = self.write("broken.py", "def calculate():\n    return totally_unknown_name\n")
        if not ls.server_command(self.root, "python"):
            self.skipTest("Python language server not installed")
        self.addCleanup(ls.close_all)
        report = ls.inspect_file(self.root, target)
        self.assertTrue(report["complete"], report)
        self.assertTrue(any("undefined" in d.get("message", "").lower() for d in report["diagnostics"]), report)


class AutonomousVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unrepaired_failure_stays_visible_in_the_final_reply(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder); root = base / "project"; root.mkdir()
            (root / "app.py").write_text("value = 1\n")
            (root / "test_app.py").write_text("import json, unittest\nfrom app import value\nclass TestValue(unittest.TestCase):\n def test_value(self): self.assertEqual(value, 300)\n")
            chat = {"id": "verify-unrepaired", "project_id": "p", "mode": "agentic", "messages": [{"role": "user", "content": "Fix the value"}]}
            settings = {"allowed_roots": [str(root)], "language_servers": False, "repo_map_enabled": False, "shell_approval_mode": "never_ask"}
            samples = 0
            async def stream(*args, **kwargs):
                nonlocal samples
                samples += 1
                if samples == 1:
                    yield {"message": {"tool_calls": [{"function": {"name": "edit_file", "arguments": {"path": "app.py", "find": "value = 1", "replace": "value = 2"}}}]}, "done": True}
                else:
                    yield {"message": {"content": "All tests passed."}, "done": True}
            with patch.object(repo_index, "INDEX_DIR", base / "indexes"), patch.object(pc, "COMMAND_DIR", base / "commands"), patch.object(tc, "CHANGE_DIR", base / "changes"), \
                 patch.object(server.st, "get_project", return_value={"root": str(root), "name": "Auto"}), \
                 patch.object(server.st, "load_settings", return_value=settings), patch.object(agent, "load_settings", return_value=settings), \
                 patch.object(server.st, "save_chat"), patch.object(server.st, "memory_as_prompt", return_value=""), \
                 patch.object(server.st, "history_index", return_value=""), patch.object(server.st, "canon_as_prompt", return_value=""), \
                 patch.object(server.oc, "stream_chat", side_effect=stream):
                events = [event async for event in server._agent_loop(chat, {"id": "test", "tools": True, "keep_alive": "0"}, "test")]
            # One repair round from the failing check, then one for claiming "passed" against it.
            self.assertEqual(samples, 4)
            self.assertTrue(chat["messages"][-1]["content"].startswith("This task is incomplete"))
            self.assertIn("A check still fails after the last change.", chat["messages"][-1]["content"])
            self.assertIn("✗", chat["messages"][-1]["content"])
            self.assertEqual([event["failed"] for event in events if event["type"] == "verification"], [1])

    async def test_failed_automatic_check_repairs_source_without_inventing_a_shell_call(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder); root = base / "project"; root.mkdir()
            (root / "app.py").write_text("value = 1\n")
            (root / "test_app.py").write_text("import unittest\nfrom app import value\nclass TestValue(unittest.TestCase):\n def test_value(self): self.assertEqual(value, 300)\n")
            chat = {"id": "verify-failure", "project_id": "p", "mode": "agentic", "messages": [{"role": "user", "content": "Fix the value"}]}
            settings = {"allowed_roots": [str(root)], "auto_verify_project": True, "language_servers": False, "repo_map_enabled": False, "semantic_search": False, "task_change_review": True, "shell_approval_mode": "never_ask"}
            samples = []
            async def stream(*args, **kwargs):
                samples.append(args[1])
                if len(samples) in (1, 3):
                    before, after = (1, 2) if len(samples) == 1 else (2, 300)
                    if len(samples) == 3:
                        self.assertIn("olith_verification", str(samples[-1]))
                        self.assertNotIn(agent.INTERNAL_VERIFICATION_COMMAND, str(samples[-1]))
                    yield {"message": {"tool_calls": [{"function": {"name": "edit_file", "arguments": {"path": "app.py", "find": f"value = {before}", "replace": f"value = {after}"}}}]}, "done": True}
                else:
                    yield {"message": {"content": "Changed the value."}, "done": True}
            with patch.object(repo_index, "INDEX_DIR", base / "indexes"), patch.object(pc, "COMMAND_DIR", base / "commands"), patch.object(tc, "CHANGE_DIR", base / "changes"), \
                 patch.object(server.st, "get_project", return_value={"root": str(root), "name": "Auto"}), \
                 patch.object(server.st, "load_settings", return_value=settings), patch.object(agent, "load_settings", return_value=settings), \
                 patch.object(server.st, "save_chat"), patch.object(server.st, "memory_as_prompt", return_value=""), \
                 patch.object(server.st, "history_index", return_value=""), patch.object(server.st, "canon_as_prompt", return_value=""), \
                 patch.object(server.oc, "stream_chat", side_effect=stream):
                events = [event async for event in server._agent_loop(chat, {"id": "test", "tools": True, "keep_alive": "0"}, "test")]
            self.assertEqual((root / "app.py").read_text(), "value = 300\n")
            self.assertEqual(len(samples), 4)
            self.assertEqual([event["failed"] for event in events if event["type"] == "verification"], [1, 0])

    async def test_agent_discovers_new_project_tests_and_verifies_before_debrief(self):
        import server
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / "project"
            root.mkdir()
            (root / "app.py").write_text("value = 1\n")
            (root / "tests").mkdir()
            (root / "tests/test_app.py").write_text("import unittest\nclass TestApp(unittest.TestCase):\n    def test_value(self):\n        self.assertTrue(True)\n")
            chat = {"id": "auto-tools", "mode": "agentic", "project_id": "p", "messages": [{"role": "user", "content": "Change value to 2"}]}
            settings = {"allowed_roots": [], "shell_approval_mode": "safe_auto", "repo_map_enabled": False, "language_servers": False}
            samples = []
            async def stream(model, messages, **kwargs):
                samples.append(messages)
                if len(samples) == 1:
                    yield {"message": {"tool_calls": [{"function": {"name": "edit_file", "arguments": {"path": "app.py", "find": "value = 1", "replace": "value = 2"}}}]}, "done": True}
                else:
                    yield {"message": {"content": "Changed and verified."}, "done": True}
            with patch.object(repo_index, "INDEX_DIR", base / "indexes"), patch.object(pc, "COMMAND_DIR", base / "commands"), patch.object(tc, "CHANGE_DIR", base / "changes"), \
                 patch.object(server.st, "get_project", return_value={"root": str(root), "name": "Auto"}), \
                 patch.object(server.st, "load_settings", return_value=settings), patch.object(agent, "load_settings", return_value=settings), \
                 patch.object(server.st, "save_chat"), patch.object(server.st, "memory_as_prompt", return_value=""), \
                 patch.object(server.st, "history_index", return_value=""), patch.object(server.st, "canon_as_prompt", return_value=""), \
                 patch.object(server.oc, "stream_chat", side_effect=stream):
                events = [event async for event in server._agent_loop(chat, {"id": "test", "tools": True, "keep_alive": "0"}, "test")]
            self.assertTrue(any(e["type"] == "verification" for e in events), events)
            self.assertEqual(chat["verification"]["commands"][0]["result"]["exit_code"], 0)
            self.assertEqual(chat["change_review"]["files"], 1)
            self.assertEqual(len(samples), 2, "Passing checks do not require another model round")
            self.assertFalse(any(agent.is_internal_verification_command(call.get("arguments", {}).get("command")) for message in chat["messages"] for call in message.get("tool_calls", [])))
            self.assertTrue(any((base / "commands").glob("*.json")))


if __name__ == "__main__":
    unittest.main()


class FileSnapshotTests(unittest.TestCase):
    def test_unchanged_files_reuse_hashes_and_touches_keep_same_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "source.py"
            path.write_text("x = 1\n")
            before = tc.FileSnapshot(root)
            with patch.object(tc.hashlib, "sha256", side_effect=AssertionError("rehashing unchanged file")):
                after = tc.FileSnapshot(root, before)
            self.assertFalse(after.partial)
            self.assertEqual(after.files, before.files)
            path.touch()
            self.assertEqual(tc.FileSnapshot(root, after).files, before.files)

    def test_unreadable_inventory_and_missing_root_are_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(tc, "_inventory", side_effect=PermissionError("denied")):
                self.assertTrue(tc.FileSnapshot(root).partial)
            self.assertTrue(tc.FileSnapshot(root / "missing").partial)

    def test_binary_extensionless_and_secret_files_are_only_fingerprinted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("image.bin", "resource", ".env", "credentials.json"):
                (root / name).write_bytes(b"secret\x00data")
            snapshot = tc.FileSnapshot(root)
            self.assertFalse(snapshot.partial)
            self.assertEqual(set(snapshot.files), {"image.bin", "resource", ".env", "credentials.json"})
            self.assertNotIn("secret", repr(snapshot.files))
            self.assertEqual(len(snapshot.files[".env"][0]), 64)
