"""Check workspace locking, base-image drift handling and snapshot pruning."""
from datetime import datetime, timedelta, timezone
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

import persistent_colab
from persistent_colab import Workspace, prune_snapshots, release_lock


def signature(*packages):
    base = [list(item) for item in packages]
    return {"python": [3, 14], "machine": "test", "os": "test", "libc": [],
            "base_packages_sha256": json.dumps(base), "base_packages": base}


BASE = signature(("numpy", "2.0"), ("torch", "2.8"))
DRIFTED = signature(("numpy", "2.1"), ("rapids", "26.6"), ("torch", "2.8"))


class RobustnessTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.signature = patch("persistent_colab.runtime_signature", return_value=BASE)
        self.signature.start()
        self.output = redirect_stdout(io.StringIO())
        self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None, None, None)
        self.signature.stop()
        for thread in threading.enumerate():
            if thread.name.startswith("colab-workspace-lock:"):
                thread.stop.set()
        self.temporary.cleanup()

    def workspace(self, local="runtime"):
        return Workspace("test", self.root / "drive", self.root / local, session="s1")

    def other_host(self):
        return patch("socket.gethostname", return_value="other-vm")

    def lock(self, ws):
        return json.loads((ws.env_dir / "LOCK").read_text())

    # Locking

    def test_lock_records_owner_and_blocks_another_runtime(self):
        ws = self.workspace().prepare()
        record = self.lock(ws)
        self.assertEqual(record["session"], "s1")
        self.assertEqual(set(record), {"session", "host", "pid", "created_utc", "heartbeat_utc"})
        ws.prepare()  # The same runtime may prepare again.
        with self.other_host(), self.assertRaisesRegex(RuntimeError, "in use by session 's1'"):
            self.workspace("runtime-b").prepare()
        self.assertFalse((self.root / "runtime-b" / "test" / ".venv").exists())

    def test_stale_lock_requires_explicit_takeover(self):
        ws = self.workspace().prepare()
        release_lock(ws.root)
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = {"session": "gone", "host": "other-vm", "pid": 1, "created_utc": old, "heartbeat_utc": old}
        (ws.env_dir / "LOCK").write_text(json.dumps(record))
        with self.assertRaisesRegex(RuntimeError, "stale.*--force-unlock"):
            self.workspace("runtime-b").prepare()
        taken = self.workspace("runtime-b").prepare(force_unlock=True)
        self.assertNotEqual(self.lock(taken)["host"], "other-vm")

    def test_unreadable_lock_is_not_overwritten_silently(self):
        ws = self.workspace()
        (ws.env_dir / "LOCK").write_text("{partial")
        with self.assertRaisesRegex(RuntimeError, "Unreadable"):
            ws.prepare()
        self.assertEqual((ws.env_dir / "LOCK").read_text(), "{partial")

    def test_release_stops_heartbeat_and_removes_only_own_lock(self):
        with patch.object(persistent_colab, "LOCK_HEARTBEAT", 0.01):
            ws = self.workspace().prepare()
            release_lock(ws.root)
        self.assertFalse(any(t.name.startswith("colab-workspace-lock:") for t in threading.enumerate()))
        self.assertFalse((ws.env_dir / "LOCK").exists())
        with self.other_host():
            self.workspace("runtime-b").prepare()
        release_lock(ws.root)
        self.assertEqual(self.lock(ws)["host"], "other-vm")

    def test_save_refuses_when_another_runtime_took_over(self):
        ws = self.workspace().prepare()
        record = {**self.lock(ws), "host": "other-vm"}
        (ws.env_dir / "LOCK").write_text(json.dumps(record))
        with self.assertRaisesRegex(RuntimeError, "not saved"):
            ws.save_environment()
        self.assertFalse(ws.latest.exists())

    def test_failed_prepare_releases_lock(self):
        ws = self.workspace().prepare()
        ws.save_environment()
        release_lock(ws.root)
        shutil.rmtree(ws.local)
        fresh = self.workspace()
        fresh.signature = {**BASE, "python": [3, 99]}
        with self.assertRaisesRegex(RuntimeError, "Python, architecture or libc"):
            fresh.prepare(allow_base_drift=True)
        self.assertFalse((ws.env_dir / "LOCK").exists())

    # Base image drift

    def saved_then_drifted(self):
        ws = self.workspace().prepare()
        (ws.venv / "lib" / "saved.txt").write_text("package")
        ws.save_environment()
        release_lock(ws.root)
        shutil.rmtree(ws.local)
        fresh = self.workspace()
        fresh.signature = DRIFTED
        return fresh

    def test_drift_is_rejected_with_package_differences(self):
        fresh = self.saved_then_drifted()
        with self.assertRaises(RuntimeError) as error:
            fresh.prepare()
        message = str(error.exception)
        self.assertIn("numpy: 2.0 -> 2.1", message)
        self.assertIn("rapids: absent -> 26.6", message)
        self.assertIn("--allow-base-drift", message)
        self.assertFalse(fresh.python.exists())

    def test_allowed_drift_restores_when_pip_check_adds_no_problems(self):
        fresh = self.saved_then_drifted()
        known = {"ipython 7.34.0 requires jedi, which is not installed."}
        with patch.object(Workspace, "_dependency_problems", return_value=known):
            fresh.prepare(allow_base_drift=True)
        self.assertEqual((fresh.venv / "lib" / "saved.txt").read_text(), "package")
        self.assertTrue((fresh.local / "environment-ready.json").exists())

    def test_allowed_drift_rolls_back_on_new_conflict(self):
        fresh = self.saved_then_drifted()
        conflicts = {"venv": {"saved 1.0 has requirement numpy<2.1"}, "base": set()}
        with patch.object(Workspace, "_dependency_problems",
                          side_effect=lambda python: conflicts["base" if str(python) != str(fresh.python) else "venv"]):
            with self.assertRaisesRegex(RuntimeError, "numpy<2.1"):
                fresh.prepare(allow_base_drift=True)
        self.assertFalse(fresh.venv.exists())
        self.assertFalse((fresh.local / "environment-ready.json").exists())

    def test_schema_one_snapshot_keeps_strict_behavior(self):
        fresh = self.saved_then_drifted()
        pointer = json.loads(fresh.latest.read_text())
        pointer["schema"] = 1
        del pointer["runtime"]["base_packages"]
        fresh.latest.write_text(json.dumps(pointer))
        with self.assertRaisesRegex(RuntimeError, "base image changed"):
            fresh.prepare(allow_base_drift=True)

    # Pruning

    def test_prune_keeps_newest_and_latest_pointer(self):
        ws = self.workspace().prepare()
        snapshots = ws.snapshots
        stamps = ["20260101T000000Z-a", "20260201T000000Z-b", "20260301T000000Z-c", "20260401T000000Z-d"]
        for stamp in stamps:
            (snapshots / (stamp + ".tar.gz")).write_bytes(b"archive")
            (snapshots / (stamp + ".json")).write_text("{}")
        ws.latest.write_text(json.dumps({"created_utc": stamps[0], "archive": stamps[0] + ".tar.gz"}))
        self.assertEqual(prune_snapshots(ws.root, keep=2), [stamps[1]])
        self.assertEqual(len(list(snapshots.iterdir())), 8)
        prune_snapshots(ws.root, keep=2, apply=True)
        remaining = sorted(path.name for path in snapshots.iterdir())
        self.assertEqual(remaining, sorted(f"{s}{ext}" for s in (stamps[0], stamps[2], stamps[3])
                                           for ext in (".json", ".tar.gz")))
        with self.assertRaises(ValueError):
            prune_snapshots(ws.root, keep=0)


if __name__ == "__main__":
    unittest.main()
