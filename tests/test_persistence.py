"""Verify archive restore, corruption detection and incompatible image handling."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from persistent_colab import Workspace, sha256


SIGNATURE = {"python": [3, 14], "machine": "test", "os": "test", "libc": [],
             "base_packages_sha256": "test"}


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.signature = patch("persistent_colab.runtime_signature", return_value=SIGNATURE)
        self.signature.start()
        self.workspace = Workspace("test", self.root / "drive", self.root / "runtime")
        self.workspace.prepare()

    def tearDown(self):
        self.signature.stop()
        self.temporary.cleanup()

    def fresh(self):
        shutil.rmtree(self.workspace.local)
        return Workspace("test", self.root / "drive", self.root / "runtime")

    def test_restore_files_environment_and_checkpoint(self):
        ws = self.workspace
        ws.write_checkpoint({"step": 42})
        payload = ws.venv / "lib" / "saved-package.txt"
        payload.write_text("package bytes survive release")
        first = ws.save_environment()
        self.assertEqual(first["sha256"], sha256(ws.snapshots / first["archive"]))
        fresh = self.fresh().prepare()
        self.assertEqual((fresh.venv / "lib" / "saved-package.txt").read_text(),
                         "package bytes survive release")
        self.assertEqual(fresh.read_checkpoint(), {"step": 42})
        self.assertEqual(fresh.run("print('restored interpreter')", capture=True).stdout.strip(),
                         "restored interpreter")

    def test_relative_roots_run_from_workspace_cwd(self):
        previous = os.getcwd()
        os.chdir(self.root)
        try:
            ws = Workspace("relative", Path("drive"), Path("rt")).prepare()
        finally:
            os.chdir(previous)
        self.assertTrue(ws.root.is_absolute() and ws.python.is_absolute())
        self.assertEqual(ws.run("print('ok')", capture=True).stdout.strip(), "ok")

    def test_corrupt_archive_is_not_accepted_or_marked_ready(self):
        ws = self.workspace
        saved = ws.save_environment()
        (ws.snapshots / saved["archive"]).write_bytes(b"broken archive")
        fresh = self.fresh()
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            fresh.prepare()
        self.assertFalse(fresh.python.exists())
        self.assertFalse((fresh.local / "environment-ready.json").exists())

    def test_base_image_change_preserves_previous_snapshot(self):
        ws = self.workspace
        saved = ws.save_environment()
        fresh = self.fresh()
        fresh.signature = {**SIGNATURE, "base_packages_sha256": "changed"}
        with self.assertRaisesRegex(RuntimeError, "base image changed"):
            fresh.prepare()
        self.assertFalse(fresh.python.exists())
        self.assertEqual(json.loads(fresh.latest.read_text()), saved)

    def test_readback_failure_keeps_old_pointer(self):
        ws = self.workspace
        saved = ws.save_environment()
        with patch("persistent_colab.sha256", side_effect=["expected", "different"]):
            with self.assertRaisesRegex(RuntimeError, "readback failed"):
                ws.save_environment()
        self.assertEqual(json.loads(ws.latest.read_text()), saved)


if __name__ == "__main__":
    unittest.main()
