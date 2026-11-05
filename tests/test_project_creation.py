# SPDX-License-Identifier: AGPL-3.0-or-later
"""Project creation uses real folders and isolated Olith storage."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import setup
import storage

try:
    import server
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    server = None


@unittest.skipIf(server is None, "Server dependencies unavailable")
class ProjectCreationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.home = self.base / "Olith"
        for patcher in (
            patch.object(config, "PROJECT_HOME", self.home),
            patch.object(storage, "PROJECTS", self.base / "data/projects"),
            patch.object(storage, "CHATS", self.base / "data/chats"),
            patch.object(storage, "MEMORY", self.base / "data/memory"),
            patch.object(storage, "SETTINGS_PATH", self.base / "data/settings.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(server.app, base_url="http://localhost")
        self.addCleanup(self.client.close)

    def create(self, **body):
        return self.client.post("/api/projects", json={"name": "My app", "create_folder": True, **body})

    def test_location_does_not_create_folders_on_read(self):
        result = self.client.get("/api/project-location")
        self.assertEqual(result.json(), {"root": str(self.home)})
        self.assertFalse(self.home.exists())

    def test_create_registers_folder_and_allows_agent_access(self):
        result = self.create(description="A fresh project")
        self.assertEqual(result.status_code, 200, result.text)
        project = result.json()
        folder = self.home / "My app"
        self.assertTrue(folder.is_dir())
        self.assertEqual(project["root"], str(folder))
        self.assertEqual(project["description"], "A fresh project")
        self.assertEqual(storage.get_project(project["id"]), project)
        self.assertIn(str(folder), storage.load_settings()["allowed_roots"])
        self.assertIn(project, self.client.get("/api/projects").json())
        tree = self.client.get(f"/api/projects/{project['id']}/tree")
        self.assertEqual(tree.status_code, 200, tree.text)
        self.assertFalse(tree.json().get("error"))

    def test_existing_folder_flow_preserves_user_files(self):
        folder = self.base / "existing"
        folder.mkdir()
        (folder / "app.py").write_text("# user source")
        result = self.create(create_folder=False, root=str(folder))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["root"], str(folder))
        self.assertEqual((folder / "app.py").read_text(), "# user source")
        self.assertFalse(self.home.exists())

    def test_duplicate_folder_and_file_are_not_overwritten(self):
        self.home.mkdir()
        for name, directory in (("directory", True), ("file", False)):
            with self.subTest(name=name):
                target = self.home / name
                if directory:
                    target.mkdir()
                    (target / "keep").write_text("user data")
                else:
                    target.write_text("user data")
                result = self.create(name=name)
                self.assertEqual(result.status_code, 409, result.text)
                self.assertEqual((target / "keep" if directory else target).read_text(), "user data")
        self.assertEqual(storage.list_projects(), [])

    def test_symlink_target_is_not_registered_or_overwritten(self):
        self.home.mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        (self.home / "linked").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.create(name="linked").status_code, 409)
        self.assertEqual(storage.list_projects(), [])

    def test_invalid_portable_names_and_traversal_are_rejected(self):
        for name in ("", "  ", ".", "..", "../outside", "one/two", "one\\two", "/tmp/project", "NUL", "con.txt", "COM1", "app.", "a:b", "bad\nname", "x" * 121):
            with self.subTest(name=name):
                result = self.create(name=name)
                self.assertEqual(result.status_code, 400, result.text)
        self.assertFalse(self.home.exists())
        self.assertEqual(storage.list_projects(), [])

    def test_existing_mode_rejects_missing_paths_and_regular_files(self):
        file = self.base / "source.py"
        file.write_text("# source")
        for root in ("", str(self.base / "missing"), str(file)):
            self.assertEqual(self.create(create_folder=False, root=root).status_code, 400)
        self.assertFalse(self.home.exists())

    def test_whitespace_is_trimmed_and_unicode_and_spaces_work(self):
        result = self.create(name="  My café  ")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["name"], "My café")
        self.assertTrue((self.home / "My café").is_dir())

    def test_creation_errors_are_actionable_and_do_not_register_projects(self):
        self.home.write_text("existing file")
        result = self.create()
        self.assertEqual(result.status_code, 400, result.text)
        self.assertIn("Could not prepare Olith's projects folder", result.json()["detail"])
        self.assertEqual(storage.list_projects(), [])
        self.assertEqual(self.home.read_text(), "existing file")

    def test_cross_origin_writes_are_rejected_before_creating_folder(self):
        result = self.client.post("/api/projects", json={"name": "evil", "create_folder": True}, headers={"origin": "https://example.invalid"})
        self.assertEqual(result.status_code, 403)
        self.assertFalse(self.home.exists())

    def test_removing_project_registration_preserves_its_folder(self):
        project = self.create().json()
        source = Path(project["root"]) / "keep.txt"
        source.write_text("keep")
        result = self.client.delete(f"/api/projects/{project['id']}")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(source.read_text(), "keep")


class InstallerProjectHomeTests(unittest.TestCase):
    def test_installer_creates_configured_folder_and_preserves_existing_projects(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "Olith"
            with patch.object(config, "PROJECT_HOME", home), patch.object(setup, "log"):
                setup.ensure_project_home()
                source = home / "My app" / "app.py"
                source.parent.mkdir()
                source.write_text("# user source")
                setup.ensure_project_home()
                self.assertEqual(source.read_text(), "# user source")

    def test_installation_fails_clearly_when_projects_folder_cannot_be_created(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "Olith"
            file.write_text("keep")
            with patch.object(config, "PROJECT_HOME", file), patch.object(setup.sys, "stderr", io.StringIO()) as err:
                with self.assertRaises(SystemExit):
                    setup.ensure_project_home()
                self.assertIn("Could not create Olith's projects folder", err.getvalue())
            self.assertEqual(file.read_text(), "keep")

    def test_common_installer_entrypoint_creates_projects_folder_with_models_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "install"
            home = Path(temp) / "Olith"
            with (
                patch.object(setup, "ROOT", root),
                patch.object(config, "PROJECT_HOME", home),
                patch.object(setup.sys, "argv", ["setup.py", "--skip-models"]),
                patch.object(setup, "ensure_venv", return_value=Path("python")),
                patch.object(setup, "pip_install"),
                patch.object(setup, "install_language_servers"),
                patch.object(setup, "find_ollama", return_value=None),
                patch.object(setup, "install_desktop_entry"),
                patch.object(setup.sys, "stdout", io.StringIO()),
            ):
                setup.main()
            self.assertTrue(home.is_dir())
            self.assertTrue((root / "data/chats").is_dir())
