"""Exercise filesystem layers with fake roots, without an account or installers."""
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from persistent_colab import SystemLayer, Workspace, _prune_system_layers


class SystemTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fs = self.root / "vm-a"
        self.base = self.root / "base"
        self.data = self.root / "drive"
        self.project = self.data / "spaces/alpha/workspaces/demo"
        self.packages = {"apt": {"base": "1"}, "pip": {"base-python": "1"}}
        for name in ("usr/local/bin/base", "usr/lib/python/base-1.dist-info/METADATA", "etc/base.conf"):
            self.put(self.fs, name, "base")
        shutil.copytree(self.fs, self.base)
        self.start = time.time() + 0.01
        self.layer = self.make(self.fs, "a")
        self.output = redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.layer.restore()

    def tearDown(self):
        self.output.__exit__(None, None, None)
        self.temporary.cleanup()

    @staticmethod
    def put(root, name, text):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def make(self, fs, name, image="image-one", project=None, mounts=None):
        return SystemLayer(project or self.project, self.data, self.root / ("local-" + name),
                           fs_root=fs, image_id=image, start_time=self.start,
                           packages=lambda: self.packages, mounts=mounts)

    def fresh(self, image="image-one", project=None):
        fs = self.root / "vm-b"
        shutil.copytree(self.base, fs)
        return self.make(fs, "b", image, project)

    def change(self, name="usr/local/bin/custom", text="saved binary"):
        # The clock injection makes the pristine fake image older than container start.
        self.layer.start = 0
        return self.put(self.fs, name, text)

    def manifest(self, pointer):
        return json.loads((self.layer.layers / (pointer["chain"][-1]["id"] + ".json")).read_text())

    def test_restore_unpacks_files_modes_links_and_package_records_without_installers(self):
        binary = self.change()
        binary.chmod(0o755)
        (binary.parent / "alias").symlink_to("custom")
        self.packages = {"apt": {"base": "1", "libdemo": "2"}, "pip": {"base-python": "2"}}
        saved = self.layer.save()
        fresh = self.fresh()
        with patch("subprocess.run", side_effect=AssertionError("restore ran an installer")):
            fresh.restore()
        self.assertEqual((fresh.fs / "usr/local/bin/custom").read_text(), "saved binary")
        self.assertEqual((fresh.fs / "usr/local/bin/custom").stat().st_mode & 0o777, 0o755)
        self.assertEqual(os.readlink(fresh.fs / "usr/local/bin/alias"), "custom")
        self.assertEqual(saved["packages"], {"apt": {"libdemo": "2"}, "pip": {"base-python": "2"}})

    def test_base_upgrade_deletions_remove_old_dist_info(self):
        self.change("usr/lib/python/base-2.dist-info/METADATA", "new version")
        shutil.rmtree(self.fs / "usr/lib/python/base-1.dist-info")
        saved = self.layer.save()
        self.assertIn("/usr/lib/python/base-1.dist-info/METADATA", self.manifest(saved)["deleted"])
        fresh = self.fresh()
        fresh.restore()
        self.assertFalse((fresh.fs / "usr/lib/python/base-1.dist-info").exists())
        self.assertTrue((fresh.fs / "usr/lib/python/base-2.dist-info/METADATA").exists())

    def test_incremental_chain_includes_only_changed_files_and_deletions(self):
        self.change()
        self.layer.save()
        self.change("opt/library.so", "small library")
        (self.fs / "usr/local/bin/custom").unlink()
        second = self.layer.save()
        self.assertEqual(len(second["chain"]), 2)
        manifest = self.manifest(second)
        self.assertNotIn("/etc/base.conf", manifest["files"])
        self.assertIn("/usr/local/bin/custom", manifest["deleted"])
        fresh = self.fresh()
        fresh.restore()
        self.assertFalse((fresh.fs / "usr/local/bin/custom").exists())
        self.assertEqual((fresh.fs / "opt/library.so").read_text(), "small library")
        fresh.save()
        self.assertEqual(json.loads((fresh.layers / (json.loads(fresh.latest.read_text())["chain"][-1]["id"] + ".json")).read_text())["files"], [])

    def test_derived_state_matches_scan_and_next_save_is_exact(self):
        self.change()
        self.change("opt/deleted-later", "remove after restore")
        self.layer.start = (self.fs / "etc/base.conf").stat().st_ctime_ns / 1e9 + 0.000001
        self.layer.save()
        self.change("usr/local/bin/custom", "second layer")
        self.change("opt/removed-by-layer", "temporary")
        self.layer.save()
        (self.fs / "opt/removed-by-layer").unlink()
        self.layer.save()
        fresh = self.fresh()
        with patch.object(fresh, "scan", side_effect=AssertionError("restore walked roots")):
            fresh.restore()
        from persistent_colab import _read_gzip_json
        state = _read_gzip_json(fresh.state_path)
        self.assertEqual(set(state["paths"]), set(fresh.scan()))
        self.assertIsNone(state["paths"]["/etc/base.conf"])
        self.assertEqual(state["paths"]["/usr/local/bin/custom"], fresh.scan()["/usr/local/bin/custom"])
        before = fresh.scan()
        self.put(fresh.fs, "usr/local/bin/custom", "later edit")
        self.put(fresh.fs, "opt/new-file", "new")
        (fresh.fs / "opt/deleted-later").unlink()
        base = fresh.fs / "etc/base.conf"
        old = base.stat()
        base.write_text("edit preserving mtime")
        os.utime(base, ns=(old.st_atime_ns, old.st_mtime_ns))
        base.chmod(0o600)
        (fresh.fs / "usr/local/bin/new-link").symlink_to("custom")
        current = fresh.scan()
        expected = {name for name, value in current.items() if before.get(name) != value}
        saved = fresh.save()
        manifest = json.loads((fresh.layers / (saved["chain"][-1]["id"] + ".json")).read_text())
        self.assertEqual(set(manifest["files"]), expected)
        self.assertEqual(set(manifest["deleted"]), set(before) - set(current))
        fresh.save()
        manifest = json.loads((fresh.layers / (json.loads(fresh.latest.read_text())["chain"][-1]["id"] + ".json")).read_text())
        self.assertEqual(manifest["files"], [])
        self.assertEqual(manifest["deleted"], [])

    def test_runtime_paths_absent_from_baseline_are_captured_on_next_save(self):
        self.change()
        self.layer.save()
        fresh = self.fresh()
        self.put(fresh.fs, "opt/runtime-created", "not in image manifest")
        fresh.restore()
        saved = fresh.save()
        manifest = json.loads((fresh.layers / (saved["chain"][-1]["id"] + ".json")).read_text())
        self.assertIn("/opt/runtime-created", manifest["files"])

    def test_derived_baseline_respects_new_runtime_mounts(self):
        self.change()
        self.layer.start = (self.fs / "etc/base.conf").stat().st_ctime_ns / 1e9 + 0.000001
        self.layer.save()
        fresh = self.fresh()
        fresh.mounts.add("/usr/lib/python")
        fresh.restore()
        from persistent_colab import _read_gzip_json
        paths = _read_gzip_json(fresh.state_path)["paths"]
        self.assertFalse(any(name.startswith("/usr/lib/python/") for name in paths))

    def test_pre_restore_directory_to_symlink_drift_refuses_bad_deletions(self):
        self.change()
        self.layer.start = (self.fs / "etc/base.conf").stat().st_ctime_ns / 1e9 + 0.000001
        self.layer.save()
        fresh = self.fresh()
        parent = fresh.fs / "usr/lib/python"
        shutil.rmtree(parent)
        parent.symlink_to("elsewhere")
        fresh.restore()
        with self.assertRaisesRegex(RuntimeError, "topology changed before restore"):
            fresh.save()

    def test_baseline_change_at_checkpoint_clock_tick_is_not_missed(self):
        from persistent_colab import _read_gzip_json, _write_gzip_json
        self.change()
        self.layer.start = (self.fs / "etc/base.conf").stat().st_ctime_ns / 1e9 + 0.000001
        self.layer.save()
        fresh = self.fresh()
        fresh.restore()
        base = fresh.fs / "etc/base.conf"
        base.write_text("changed at checkpoint tick")
        state = _read_gzip_json(fresh.state_path)
        self.assertIsNone(state["paths"]["/etc/base.conf"])
        state["baseline_before_ns"] = base.lstat().st_ctime_ns
        _write_gzip_json(fresh.state_path, state)
        saved = fresh.save()
        manifest = json.loads((fresh.layers / (saved["chain"][-1]["id"] + ".json")).read_text())
        self.assertIn("/etc/base.conf", manifest["files"])

    def test_pointer_failure_preserves_local_state_for_retry(self):
        from persistent_colab import write_json, _read_gzip_json
        self.change()
        before = _read_gzip_json(self.layer.state_path)
        def fail_pointer(path, value):
            if path == self.layer.latest:
                raise OSError("Drive pointer unavailable")
            write_json(path, value)
        with patch("persistent_colab.write_json", side_effect=fail_pointer):
            with self.assertRaisesRegex(OSError, "Drive pointer"):
                self.layer.save()
        self.assertEqual(_read_gzip_json(self.layer.state_path), before)
        self.layer.save()

    def test_timings_are_recorded_and_baseline_drift_is_not_cached(self):
        saved = self.layer.save()
        self.assertIn("scan", saved["phases"])
        metrics = json.loads((self.layer.local / "system-restore.json").read_text())
        self.assertIn("state_derive", metrics["phases"])
        with self.layer.baseline_path.open("ab") as stream:
            stream.write(b"tampered")
        with self.assertRaisesRegex(RuntimeError, "baseline checksum"):
            self.layer.save()

    def test_excludes_credentials_runtime_state_volatile_files_and_bind_mounts(self):
        self.change()
        forbidden = ["root/.config/rclone/rclone.conf", "root/.config/colab-workspace/rclone-mount.json",
                     "root/.local/share/jupyter/runtime/kernel.json", "etc/ssh/ssh_host_rsa_key",
                     "etc/shadow", "var/lib/apt/lists/private", "opt/colab-workspace/state",
                     "usr/lib/host-driver/libcuda.so", "tmp/credentials", "content/runtime",
                     "usr/lib/package/__pycache__/cached.pyc"]
        for name in forbidden:
            self.put(self.fs, name, "secret-sentinel")
        self.layer.mounts.add("/usr/lib/host-driver")
        saved = self.layer.save()
        manifest = self.manifest(saved)
        with tarfile.open(self.layer.layers / (saved["chain"][-1]["id"] + ".tar.gz")) as archive:
            contents = b"".join(archive.extractfile(m).read() for m in archive if m.isfile())
        self.assertNotIn(b"secret-sentinel", contents)
        for name in forbidden:
            self.assertNotIn("/" + name, manifest["files"])

    def test_initial_capture_uses_ctime_not_old_mtime(self):
        path = self.change()
        os.utime(path, (1, 1))
        saved = self.layer.save()
        self.assertIn("/usr/local/bin/custom", self.manifest(saved)["files"])

    def test_checksum_failure_never_publishes_pointer(self):
        self.change()
        saved = self.layer.save()
        with patch.object(self.layer, "_copy_verified", side_effect=RuntimeError("readback failed")):
            with self.assertRaisesRegex(RuntimeError, "readback failed"):
                self.layer.save()
        self.assertEqual(json.loads(self.layer.latest.read_text()), saved)

    def test_corrupt_later_layer_is_checked_before_any_files_are_restored(self):
        self.change()
        self.layer.save()
        self.change("opt/library.so")
        saved = self.layer.save()
        (self.layer.layers / (saved["chain"][-1]["id"] + ".tar.gz")).write_bytes(b"corrupt")
        fresh = self.fresh()
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            fresh.restore()
        self.assertFalse((fresh.fs / "usr/local/bin/custom").exists())

    def test_image_mismatch_lists_recipe_and_does_not_apply_layers(self):
        self.change()
        self.packages = {"apt": {"base": "1", "libdemo": "2"}, "pip": {"base-python": "2"}}
        self.layer.save()
        fresh = self.fresh(image="image-two")
        with self.assertRaisesRegex(RuntimeError, "libdemo.*rebuild-system"):
            # Match across the explanatory newline.
            try:
                fresh.restore()
            except RuntimeError as error:
                raise RuntimeError(str(error).replace("\n", " "))
        self.assertFalse((fresh.fs / "usr/local/bin/custom").exists())

    def test_second_project_is_refused_even_without_saved_layers(self):
        other = self.make(self.fs, "other", project=self.data / "spaces/beta/workspaces/demo")
        with self.assertRaisesRegex(RuntimeError, "Another project's"):
            other.restore()

    def test_compact_retains_old_layers_and_restores_one_merged_layer(self):
        self.change()
        first = self.layer.save()
        self.change("opt/library.so", "library")
        self.layer.save()
        compact = self.layer.save(compact=True)
        self.assertEqual(len(compact["chain"]), 1)
        self.assertEqual(len(list(self.layer.layers.glob("*.tar.gz"))), 3)
        self.assertTrue((self.layer.layers / (first["chain"][0]["id"] + ".json")).exists())
        fresh = self.fresh()
        fresh.restore()
        self.assertEqual((fresh.fs / "opt/library.so").read_text(), "library")
        self.assertTrue((fresh.fs / "usr/local/bin/custom").exists())

    def test_tampered_manifest_is_rejected(self):
        self.change()
        saved = self.layer.save()
        (self.layer.layers / (saved["chain"][0]["id"] + ".json")).write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "manifest checksum"):
            self.fresh().restore()

    def test_explicit_rebuild_starts_new_image_chain_and_preserves_old_layers(self):
        self.change()
        self.packages = {"apt": {"base": "1", "libdemo": "2"}, "pip": {"base-python": "2"}}
        old = self.layer.save()
        self.packages = {"apt": {"base": "1"}, "pip": {"base-python": "1"}}
        fresh = self.fresh(image="image-two")
        def installed(command, **options):
            self.put(fresh.fs, "opt/rebuilt", "reinstalled")
            self.packages = {"apt": {"base": "1", "libdemo": "2"}, "pip": {"base-python": "2"}}
        with patch("subprocess.run", side_effect=installed) as run:
            rebuilt = fresh.rebuild()
        self.assertEqual(rebuilt["image_id"], "image-two")
        self.assertEqual(len(rebuilt["chain"]), 1)
        self.assertIn("libdemo=2", run.call_args_list[1].args[0])
        self.assertIn("base-python==2", run.call_args_list[2].args[0])
        self.assertTrue((self.layer.layers / (old["chain"][0]["id"] + ".tar.gz")).exists())

    def test_prune_dry_run_and_explicit_removal_preserve_current_chain(self):
        self.change()
        old = self.layer.save()
        self.change("opt/library.so")
        self.layer.save()
        current = self.layer.save(compact=True)
        _prune_system_layers(self.project, 1, False, set())
        self.assertEqual(len(list(self.layer.layers.glob("*.json"))), 3)
        _prune_system_layers(self.project, 1, True, set())
        self.assertFalse((self.layer.layers / (old["chain"][0]["id"] + ".json")).exists())
        self.assertEqual([p.stem for p in self.layer.layers.glob("*.json")], [current["chain"][0]["id"]])

    def test_archive_cannot_write_through_a_symlink_parent(self):
        self.change()
        saved = self.layer.save()
        fresh = self.fresh()
        shutil.rmtree(fresh.fs / "usr/local/bin")
        outside = self.root / "outside"
        outside.mkdir()
        (fresh.fs / "usr/local/bin").symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "symlink parent"):
            fresh.restore()
        self.assertEqual(list(outside.iterdir()), [])


class SpacesTest(unittest.TestCase):
    def test_notebook_and_cli_roots_are_identical_and_spaces_are_separate(self):
        with tempfile.TemporaryDirectory() as directory, patch("persistent_colab.runtime_signature", return_value={}):
            root = Path(directory).resolve()
            alpha = Workspace("demo", root / "drive", root / "local", space="alpha", system=False)
            again = Workspace("demo", root / "drive", root / "local", space="alpha", system=False)
            beta = Workspace("demo", root / "drive", root / "local", space="beta", system=False)
            self.assertEqual(alpha.root, again.root)
            self.assertEqual(alpha.root, root / "drive/spaces/alpha/workspaces/demo")
            self.assertNotEqual(alpha.local, beta.local)
            alpha.write_checkpoint({"space": "alpha"})
            self.assertIsNone(beta.read_checkpoint())
            with self.assertRaises(ValueError):
                Workspace("demo", root / "drive", root / "local", space="../escape")


if __name__ == "__main__":
    unittest.main()
