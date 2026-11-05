# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI account checks and real subprocess dispatch through connection_call."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import cli_tools
import mcp_connections as mc
from connection_catalog import CLI_PROVIDERS, preferred_tools
from tests.test_mcp_connections import ConnectionCase

PROGRAM = '''#!{python}
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
if 'whoami' in args:
    print('Not logged in' if os.environ.get('CLI_FIXTURE_MODE') == 'logged_out' else '{{"username":"fixture-user"}}')
elif args == ['--help']:
    print('Usage: fixture projects, deploy, logs')
elif args[0] == 'sleep':
    time.sleep(5)
    Path(args[1]).write_text('finished')
else:
    print(json.dumps({{'args': args, 'cwd': os.getcwd(), 'token': os.environ.get('VERCEL_TOKEN', '')}}))
'''


class CLIConnectionTests(ConnectionCase):
    def setUp(self):
        super().setUp()
        self.program = self.base / "fake vercel"
        self.program.write_text(PROGRAM.format(python=sys.executable))
        self.program.chmod(0o755)
        which = patch.object(cli_tools, "_binary", return_value=str(self.program))
        which.start(); self.addCleanup(which.stop)

    def save(self, **extra):
        return mc.save({"id": "vercel-cli", "label": "Vercel CLI", "transport": "cli", "provider": "vercel", **extra})

    def connect(self):
        self.save()
        result = asyncio.run(mc.test_connection("vercel-cli", interactive=True))
        self.assertEqual(result["status"], "connected", result)
        return result

    def test_a_cli_account_is_verified_before_tools_become_available(self):
        self.save()
        self.assertEqual(mc.active(), [])
        result = asyncio.run(mc.test_connection("vercel-cli", interactive=True))
        self.assertEqual(result["status"], "connected", result)
        self.assertEqual(result["tool_count"], 2)
        self.assertIn("CLI login verified", result["note"])
        self.assertEqual(self.vault, {})
        self.assertIn("vercel-cli", mc.discover({})["servers"][0]["id"])

    def test_a_logged_out_cli_cannot_be_confirmed_manually(self):
        self.save()
        with patch.dict(os.environ, CLI_FIXTURE_MODE="logged_out"):
            result = asyncio.run(mc.test_connection("vercel-cli", interactive=True))
        self.assertEqual(result["status"], "needs attention")
        self.assertIn("vercel login", result["note"])
        self.assertEqual(mc.active(), [])

    def test_noninteractive_connection_tests_also_verify_cli_authentication(self):
        self.save()
        with patch.dict(os.environ, CLI_FIXTURE_MODE="logged_out"):
            self.assertEqual(asyncio.run(mc.test_connection("vercel-cli"))["status"], "needs attention")

    def test_a_missing_cli_reports_install_and_login_instructions(self):
        self.save()
        with patch.object(cli_tools, "_binary", return_value=None):
            result = asyncio.run(mc.test_connection("vercel-cli", interactive=True))
        self.assertEqual(result["status"], "needs attention")
        self.assertIn("npm install -g vercel", result["note"])
        self.assertIn("vercel login", result["note"])

    def test_commands_use_literal_arguments_and_the_reviewed_project_directory(self):
        self.connect()
        marker = self.base / "unexpected"
        args = {"server": "vercel-cli", "tool": "vercel_command", "arguments": {
            "args": ["projects", f"$(touch {marker})", "a; echo unsafe"], "cwd": str(self.base)}}
        refused = asyncio.run(mc.call_async(args))
        self.assertTrue(refused["needs_approval"])
        result = asyncio.run(mc.call_async(args, approved=True, ticket=mc.approval_ticket(args)))
        self.assertTrue(result["ok"], result)
        output = json.loads(result["structuredContent"]["stdout"])
        self.assertEqual(output["args"], args["arguments"]["args"])
        self.assertEqual(output["cwd"], str(self.base))
        self.assertFalse(marker.exists())

    def test_help_uses_the_installed_cli_without_an_action_approval(self):
        self.connect()
        result = asyncio.run(mc.call_async({"server": "vercel-cli", "tool": "vercel_help", "arguments": {}}))
        self.assertTrue(result["ok"], result)
        self.assertIn("Usage:", result["structuredContent"]["stdout"])

    def test_authentication_and_credential_dump_commands_are_not_agent_tools(self):
        self.connect()
        for command in (["auth", "token"], ["login"], ["config", "view"], ["projects", "--show-token"],
                        ["projects", "--token=private"], ["deploy", "--cwd=/other/project"]):
            with self.subTest(command=command):
                result = asyncio.run(mc.call_async({"server": "vercel-cli", "tool": "vercel_command",
                    "arguments": {"args": command, "cwd": str(self.base)}}, approved=True))
                self.assertIn("Authenticate or manage credentials yourself", result["error"])

    def test_known_environment_credentials_are_redacted_from_cli_output(self):
        self.connect()
        with patch.dict(os.environ, VERCEL_TOKEN="private-fixture-credential"):
            result = asyncio.run(mc.call_async({"server": "vercel-cli", "tool": "vercel_command",
                "arguments": {"args": ["projects"], "cwd": str(self.base)}}, approved=True))
        self.assertNotIn("private-fixture-credential", json.dumps(result))
        self.assertIn("[redacted]", json.dumps(result))

    def test_commands_require_an_existing_absolute_directory(self):
        self.connect()
        result = asyncio.run(mc.call_async({"server": "vercel-cli", "tool": "vercel_command",
            "arguments": {"args": ["projects"], "cwd": "relative/path"}}, approved=True))
        self.assertIn("absolute project directory", result["error"])

    def test_cli_credentials_cannot_be_copied_into_olith(self):
        with self.assertRaises(ValueError):
            self.save(token="should-not-be-saved")
        self.assertEqual(self.vault, {})

    def test_unknown_cli_providers_are_rejected(self):
        with self.assertRaises(ValueError):
            self.save(provider="arbitrary-executable")

    def test_pausing_a_cli_connection_removes_its_agent_access(self):
        self.connect()
        mc.set_enabled("vercel-cli", False)
        self.assertEqual(mc.discover({})["servers"], [])
        args = {"server": "vercel-cli", "tool": "vercel_help", "arguments": {}}
        self.assertIn("not enabled and connected", mc.preflight(args)["error"])
        self.assertIn("error", asyncio.run(mc.call_async(args)))

    def test_command_timeout_terminates_the_process(self):
        marker = self.base / "late-command"
        async def run():
            with self.assertRaises(TimeoutError):
                await cli_tools.execute("vercel", ["sleep", str(marker)], self.base, timeout=0.1)
        asyncio.run(run())
        self.assertFalse(marker.exists())

    def test_cancelling_a_command_terminates_its_process(self):
        marker = self.base / "cancelled-command"
        async def run():
            task = asyncio.create_task(cli_tools.execute("vercel", ["sleep", str(marker)], self.base))
            await asyncio.sleep(0.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(run())
        self.assertFalse(marker.exists())

    def test_empty_authenticated_project_lists_remain_valid_for_account_checks(self):
        for provider, output in (("supabase", "[]"), ("atlassian", "[]"), ("atlassian", '{"values":[]}'), ("google", '{"files":[]}')):
            with self.subTest(provider=provider):
                with patch.object(cli_tools, "execute", AsyncMock(return_value={"exit_code": 0, "stdout": output, "stderr": ""})):
                    asyncio.run(cli_tools.verify(provider))

    def test_windows_npm_shims_are_resolved_without_a_shell(self):
        shim = self.base / "vercel.cmd"; shim.touch()
        package = self.base / "node_modules" / "vercel"
        package.mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"bin": {"vercel": "index.js"}}))
        script = package / "index.js"; script.write_text("// fixture")
        with patch.object(cli_tools, "_binary", return_value=str(shim)), \
             patch.object(cli_tools.shutil, "which", return_value="node.exe"):
            self.assertEqual(cli_tools.launcher("vercel", windows=True), ["node.exe", str(script)])

    def test_all_supported_clis_are_preferred_to_browser_sign_ins(self):
        for provider in CLI_PROVIDERS:
            self.assertEqual(preferred_tools(provider), provider + "-cli")
        self.assertEqual(preferred_tools("notion"), "notion")
        self.assertIsNone(preferred_tools("custom-website"))

    def test_probe_success_requires_positive_authentication_evidence(self):
        for provider in CLI_PROVIDERS:
            for output in ("", "Not logged in", '{"error":"unauthorized"}', '{}', 'Usage: provider COMMAND [options]'):
                with self.subTest(provider=provider, output=output):
                    with patch.object(cli_tools, "execute", AsyncMock(return_value={"exit_code": 0, "stdout": output, "stderr": ""})):
                        with self.assertRaises(ValueError):
                            asyncio.run(cli_tools.verify(provider))


class CLIDiscoveryTests(unittest.TestCase):
    def test_desktop_path_can_find_standard_user_installs(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            command = home / ".local/bin/vercel"
            command.parent.mkdir(parents=True); command.touch(); command.chmod(0o755)
            with patch.object(cli_tools.shutil, "which", return_value=None), patch.object(cli_tools.Path, "home", return_value=home):
                self.assertEqual(cli_tools._binary("vercel"), str(command))

    @unittest.skipIf(os.name == "nt", "nvm Unix layout")
    def test_nvm_install_is_discovered_when_the_desktop_does_not_source_the_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            command = home / ".nvm/versions/node/v24.1.0/bin/vercel"
            command.parent.mkdir(parents=True); command.touch(); command.chmod(0o755)
            with patch.object(cli_tools.shutil, "which", return_value=None), patch.object(cli_tools.Path, "home", return_value=home), \
                 patch.dict(os.environ, {"NVM_DIR": str(home / ".nvm")}):
                self.assertEqual(cli_tools._binary("vercel"), str(command))
