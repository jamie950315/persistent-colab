"""Check the rclone backend's flush contract with fake rclone/fusermount executables."""
import json
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import persistent_colab
from persistent_colab import Workspace, flush_rclone, mount_rclone


SIGNATURE = {"python": [3, 14], "machine": "test", "os": "test", "libc": [],
             "base_packages_sha256": "test"}

# Remote paths "fake:PC/<relative>" resolve under FAKE_REMOTE_ROOT.
FAKE_RCLONE = """\
import hashlib, json, os, sys
from pathlib import Path
args = sys.argv[3:]
def local(remote):
    return Path(os.environ['FAKE_REMOTE_ROOT']) / remote.split(':', 1)[1].removeprefix('PC').lstrip('/')
if args[:2] == ['rc', 'vfs/stats']:
    queue = Path(os.environ['FAKE_STATS'])
    states = json.loads(queue.read_text())
    queue.write_text(json.dumps(states[1:] or states))
    print(json.dumps({'diskCache': states[0]}))
elif args[0] == 'cat':
    sys.stdout.write(local(args[1]).read_text())
elif args[0] == 'lsjson':
    path = local(args[-1])
    print(json.dumps({'Size': path.stat().st_size,
                      'Hashes': {'md5': hashlib.md5(path.read_bytes()).hexdigest()}}))
else:
    sys.exit('unexpected rclone call: %r' % args)
"""

FAKE_FUSERMOUNT = """\
import os, sys
from pathlib import Path
Path(os.environ['FAKE_MOUNTED']).unlink()
"""


class RcloneTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.mount = self.root / "mnt"
        self.config = self.root / "config" / "rclone.conf"
        self.config.parent.mkdir()
        self.config.write_text("[fake]\ntype = alias\nremote = /secret-sentinel\n")
        self.state = self.root / "state" / "rclone-mount.json"
        self.state.parent.mkdir()
        self.state.write_text(json.dumps({"remote": "fake:PC", "mount_point": str(self.mount),
                                          "config": str(self.config), "rc": "127.0.0.1:5572"}))
        self.mounted = self.root / "mounted"
        self.mounted.touch()
        self.stats = self.root / "stats.json"
        self.stats.write_text(json.dumps([
            {"uploadsInProgress": 1, "uploadsQueued": 2, "erroredFiles": 0},
            {"uploadsInProgress": 0, "uploadsQueued": 0, "erroredFiles": 0}]))
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, source in (("rclone", FAKE_RCLONE), ("fusermount3", FAKE_FUSERMOUNT)):
            path = bin_dir / name
            path.write_text(f"#!{sys.executable}\n" + source)
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        mounted = self.mounted
        real_ismount = os.path.ismount
        self.patches = [
            patch("persistent_colab.runtime_signature", return_value=SIGNATURE),
            patch.object(persistent_colab, "RCLONE_STATE", self.state),
            patch.object(persistent_colab, "RCLONE_CONF", self.config),
            patch("os.path.ismount", side_effect=lambda p: (
                mounted.exists() if Path(p) == self.mount else real_ismount(p))),
            patch.dict(os.environ, {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                                    "FAKE_REMOTE_ROOT": str(self.mount),
                                    "FAKE_STATS": str(self.stats),
                                    "FAKE_MOUNTED": str(self.mounted)}),
        ]
        for item in self.patches:
            item.start()
        self.workspace = Workspace("demo", self.mount / "workspaces", self.root / "runtime").prepare()
        (self.workspace.venv / "lib" / "package.txt").write_text("saved")
        self.workspace.save_environment()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def test_flush_waits_verifies_unmounts_and_removes_credentials(self):
        backup = self.config.with_name("rclone.conf.old")
        backup.write_text("token copy")
        self.workspace.flush()
        self.assertFalse(self.mounted.exists())
        self.assertFalse(self.config.exists() or backup.exists() or self.state.exists())
        # The second vfs/stats response (idle) was consumed after the busy one.
        self.assertEqual(len(json.loads(self.stats.read_text())), 1)

    def test_remote_pointer_mismatch_keeps_mount_and_credentials(self):
        remote = self.root / "remote"
        (remote / "workspaces" / "demo" / "environment").mkdir(parents=True)
        (remote / "workspaces" / "demo" / "environment" / "latest.json").write_text('{"archive": "old"}')
        with patch.dict(os.environ, {"FAKE_REMOTE_ROOT": str(remote)}):
            with self.assertRaisesRegex(RuntimeError, "differs"):
                flush_rclone([self.workspace.root])
        self.assertTrue(self.mounted.exists() and self.config.exists() and self.state.exists())

    def test_failed_uploads_are_not_reported_as_flushed(self):
        self.stats.write_text(json.dumps([{"uploadsInProgress": 0, "uploadsQueued": 0, "erroredFiles": 1}]))
        with self.assertRaisesRegex(RuntimeError, "failed to upload"):
            flush_rclone([self.workspace.root])
        self.assertTrue(self.mounted.exists() and self.config.exists())

    def test_snapshot_never_contains_rclone_credentials(self):
        snapshot = self.workspace.save_environment()
        with tarfile.open(self.workspace.snapshots / snapshot["archive"]) as archive:
            members = archive.getmembers()
            contents = b"".join(archive.extractfile(m).read() for m in members if m.isfile())  # type: ignore[union-attr]
        self.assertTrue(all(m.name.split("/")[0] in ("lib", "bin", "include") for m in members))
        self.assertNotIn(b"secret-sentinel", contents)
        self.assertNotIn(b"secret-sentinel", json.dumps(snapshot).encode())

    def test_failed_mount_removes_staged_and_installed_config(self):
        staged = self.root / "staged.conf"
        staged.write_text(self.config.read_text())
        self.config.unlink()
        self.mounted.unlink()
        with patch("persistent_colab._install_rclone", side_effect=RuntimeError("no network")):
            with self.assertRaisesRegex(RuntimeError, "no network"):
                mount_rclone("fake:PC", self.mount, staged)
        self.assertFalse(staged.exists() or self.config.exists())

    def test_existing_foreign_mount_is_not_replaced(self):
        staged = self.root / "staged.conf"
        staged.write_text("new")
        with self.assertRaisesRegex(RuntimeError, "already mounted"):
            mount_rclone("other:PC", self.mount, staged)
        self.assertFalse(staged.exists())
        self.assertIn("secret-sentinel", self.config.read_text())


if __name__ == "__main__":
    unittest.main()
