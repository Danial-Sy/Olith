# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import editor_bridge as bridge


class PersistentPairings(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"; self.root.mkdir()
        self.store = Path(self.temp.name) / "pairings.json"
        for item in (patch.object(bridge, "STORE", self.store), patch.object(bridge, "_sessions", {}), patch.object(bridge, "_loaded", False)):
            item.start(); self.addCleanup(item.stop)

    def pair(self):
        return bridge.connect(bridge.pair_code()["code"], self.root)

    def restart(self):
        bridge._sessions.clear(); bridge._loaded = False

    def test_restart_keeps_authentication_but_not_heartbeat_or_pending_writes(self):
        pair = self.pair()
        bridge.session(pair["id"])["pending"]["old-edit"] = {"proposal": {"content": "private source"}, "event": threading.Event()}
        bridge._save()
        stored = self.store.read_text()
        self.assertNotIn(pair["token"], stored)
        self.assertNotIn("private source", stored)
        if os.name != "nt":
            self.assertEqual(self.store.stat().st_mode & 0o777, 0o600)
        self.restart()
        value = bridge.session(pair["id"], pair["token"])
        self.assertEqual(value["root"], self.root)
        self.assertEqual(value["pending"], {})
        self.assertFalse(bridge.status()[0]["connected"])
        with self.assertRaisesRegex(ValueError, "Invalid editor credential"):
            bridge.session(pair["id"], "wrong")
        bridge.pending(pair["id"], "0.1.6")
        self.assertTrue(bridge.status()[0]["connected"])

    def test_disconnect_stays_revoked_after_restart(self):
        pair = self.pair(); bridge.disconnect(pair["id"]); self.restart()
        with self.assertRaisesRegex(ValueError, "removed"):
            bridge.session(pair["id"], pair["token"])
        self.assertEqual(bridge.status(), [])

    def test_failed_atomic_write_does_not_grant_an_unsaved_pairing(self):
        pair = self.pair(); before = self.store.read_bytes()
        with patch.object(bridge.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): self.pair()
            with self.assertRaises(OSError): bridge.disconnect(pair["id"])
        self.assertEqual(self.store.read_bytes(), before)
        self.assertEqual(len(bridge._sessions), 1)
        self.assertEqual(list(self.store.parent.glob(".editor-pairings-*.tmp")), [])

    def test_corrupt_store_is_not_silently_overwritten(self):
        self.store.write_text("not json")
        with self.assertRaisesRegex(ValueError, "could not be read"): self.pair()
        self.assertEqual(self.store.read_text(), "not json")

    def test_pairing_survives_a_real_new_python_process(self):
        pair = self.pair()
        script = '''import json,sys
from pathlib import Path
import editor_bridge as bridge
bridge.STORE=Path(sys.argv[1])
pair=json.loads(sys.stdin.read())
value=bridge.session(pair["id"],pair["token"])
print(json.dumps({"root":str(value["root"]),"status":bridge.status()}))
'''
        result = subprocess.run([sys.executable, "-c", script, str(self.store)], input=json.dumps(pair), text=True, capture_output=True, check=True)
        restored = json.loads(result.stdout)
        self.assertEqual(restored["root"], str(self.root))
        self.assertFalse(restored["status"][0]["connected"])
