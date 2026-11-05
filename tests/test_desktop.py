# SPDX-License-Identifier: AGPL-3.0-or-later
import sys
import types
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

import desktop
import ollama_client


class DesktopDownloadTests(unittest.TestCase):
    def test_downloads_are_enabled_before_creating_either_desktop_backend(self):
        for platform, backend in (("linux", "gtk"), ("win32", "edgechromium")):
            with self.subTest(platform=platform), ExitStack() as stack:
                webview = types.ModuleType("webview")
                webview.settings = {"ALLOW_DOWNLOADS": False}
                def create_window(**kwargs):
                    self.assertTrue(webview.settings["ALLOW_DOWNLOADS"])
                    self.assertEqual(kwargs["url"], "http://127.0.0.1:7878")
                webview.create_window = Mock(side_effect=create_window)
                webview.start = Mock()
                process = Mock()
                process.poll.return_value = 0
                for target, name, value in (
                    (desktop, "_activate_existing", Mock(return_value=False)),
                    (desktop, "_record_desktop_owner", Mock()),
                    (desktop, "_windows_branding", Mock()),
                    (desktop, "_linux_branding", Mock()),
                    (desktop, "_start_server", Mock(return_value=process)),
                    (desktop, "_wait_ready", Mock()),
                    (desktop, "_free_vram", Mock()),
                    (desktop.os, "chdir", Mock()),
                    (desktop.atexit, "register", Mock()),
                    (desktop.signal, "signal", Mock()),
                    (ollama_client, "ensure_ollama_sync", Mock()),
                    (sys, "platform", platform),
                    (sys, "path", list(sys.path)),
                ):
                    stack.enter_context(patch.object(target, name, value))
                stack.enter_context(patch.dict(sys.modules, {"webview": webview}))
                desktop.main()
                webview.create_window.assert_called_once()
                self.assertEqual(webview.start.call_args.kwargs["gui"], backend)


class LinuxBrandingTests(unittest.TestCase):
    def test_linux_identity_matches_desktop_launcher(self):
        glib = Mock()
        with patch.object(sys, "platform", "linux"), patch.dict(
            sys.modules, {"gi.repository": types.SimpleNamespace(GLib=glib)}
        ), patch.dict(desktop.os.environ, {}, clear=False):
            desktop._linux_branding()
            self.assertEqual(desktop.os.environ["GDK_PROGRAM_CLASS"], "olith")
            glib.set_prgname.assert_called_once_with("olith")
            glib.set_application_name.assert_called_once_with("Olith")
        launcher = (desktop.ROOT / "Olith.desktop").read_text()
        self.assertIn("StartupWMClass=olith", launcher)
