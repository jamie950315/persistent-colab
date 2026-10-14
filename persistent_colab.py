"""Restore a Python workspace from Drive without pretending the VM is persistent."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import site
import subprocess
import sys
import tarfile
import tempfile
import uuid
from datetime import datetime, timezone


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def runtime_signature():
    # Base packages are inherited, rather than copied into each environment.
    # A changed base image requires an explicit rebuild, never a silent restore.
    packages = json.loads(subprocess.check_output(
        [sys.executable, "-m", "pip", "list", "--format=json"], text=True))
    normalized = sorted((p["name"].lower().replace("_", "-"), p["version"])
                        for p in packages)
    return {
        "python": list(sys.version_info[:2]),
        "machine": platform.machine(),
        "os": platform.system(),
        "libc": list(platform.libc_ver()),
        "base_packages_sha256": hashlib.sha256(
            json.dumps(normalized).encode()).hexdigest(),
    }


class Workspace:
    """Use durable files on Drive and executable packages on the current VM."""

    def __init__(self, project="default", drive_root=None, local_root=None):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", project):
            raise ValueError("Project must use letters, digits, underscores or hyphens.")
        default_drive = Path("/content/drive/MyDrive")
        if drive_root is None and not default_drive.is_dir():
            raise RuntimeError("Mount Google Drive before creating a workspace.")
        self.project = project
        self.root = Path(drive_root or default_drive / "PersistentColab" / "workspaces") / project
        self.local = Path(local_root or "/content/persistent-colab") / project
        self.venv = self.local / ".venv"
        self.python = self.venv / "bin" / "python"
        self.env_dir = self.root / "environment"
        self.snapshots = self.env_dir / "snapshots"
        self.latest = self.env_dir / "latest.json"
        self.signature = runtime_signature()
        self.activated = False
        for folder in ("src", "data", "models", "outputs", "checkpoints", "environment/snapshots"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        self.local.mkdir(parents=True, exist_ok=True)
        # Model files are durable, tokens remain in the ephemeral runtime.
        os.environ["HF_HUB_CACHE"] = str(self.root / "models" / "huggingface")
        os.environ["HF_TOKEN_PATH"] = str(self.local / "hf-token")
        os.environ["TORCH_HOME"] = str(self.root / "models" / "torch")

    def prepare(self, restore=True):
        """Recreate executable paths and restore the latest compatible snapshot."""
        ready = self.local / "environment-ready.json"
        if self.python.exists():
            if ready.exists() and json.loads(ready.read_text()) == self.signature:
                return self
            raise RuntimeError("Incomplete local environment. Start a fresh runtime to restore safely.")
        snapshot = json.loads(self.latest.read_text()) if restore and self.latest.exists() else None
        if snapshot and snapshot["runtime"] != self.signature:
            raise RuntimeError(
                "The Colab Python/base image changed. The saved environment was not loaded. "
                "Review environment/latest.json, then explicitly run ws.rebuild() to "
                "reinstall the saved requirements on this image. Old snapshots are kept.")
        if snapshot and snapshot["local_path"] != str(self.venv):
            raise RuntimeError("Snapshot executable paths differ. Use the original project name and runtime path.")
        # Colab's image has pip but its ensurepip bootstrap is unavailable.
        # Inherit pip from the base image; its install target is still this venv.
        subprocess.run([sys.executable, "-m", "venv", "--without-pip",
                        "--system-site-packages", str(self.venv)], check=True)
        if snapshot:
            archive_name = snapshot["archive"]
            if Path(archive_name).name != archive_name:
                raise ValueError("Invalid snapshot archive name.")
            source = self.snapshots / archive_name
            local_archive = self.local / "restore.tar.gz"
            shutil.copyfile(source, local_archive)
            if sha256(local_archive) != snapshot["sha256"]:
                shutil.rmtree(self.venv)
                local_archive.unlink()
                raise RuntimeError("Snapshot checksum mismatch; environment was not restored.")
            try:
                with tarfile.open(local_archive, "r:gz") as archive:
                    archive.extractall(self.venv, filter="data")
            except Exception:
                # This directory is an agent-owned, disposable VM environment only.
                shutil.rmtree(self.venv)
                raise
            finally:
                local_archive.unlink(missing_ok=True)
            print("Restored Python environment:", archive_name)
        else:
            print("Created a new Python environment; Colab base packages are available.")
        write_json(ready, self.signature)
        print("Persistent workspace:", self.root)
        return self

    def run(self, code, capture=False):
        """Run Python in the saved environment; persistent workspace is its cwd."""
        return subprocess.run([str(self.python), "-c", code], cwd=self.root,
                              check=True, text=True, capture_output=capture)

    def install(self, *packages):
        """Install into the environment and immediately persist its new snapshot."""
        if not packages or any(p.startswith("-") for p in packages):
            raise ValueError("Pass package specifications, such as 'humanize==4.15.0'.")
        subprocess.run([str(self.python), "-m", "pip", "install", *packages], check=True)
        result = self.save_environment()
        importlib.invalidate_caches()
        print("New packages are available in fresh imports; restart before replacing loaded packages.")
        return result

    def save_environment(self):
        """Commit an immutable archive, publishing the pointer only after readback."""
        if not self.python.exists():
            raise RuntimeError("Run ws.prepare() first.")
        requirements = subprocess.check_output(
            [str(self.python), "-m", "pip", "freeze", "--local"], text=True)
        # Do not persist credentials embedded in installation URLs.
        if "://" in requirements or "-e " in requirements or " @ " in requirements:
            raise RuntimeError("Use versioned index packages; save local/editable source under ws.root / 'src'.")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        name = stamp + ".tar.gz"
        with tempfile.TemporaryDirectory(dir=self.local) as temporary:
            archive_path = Path(temporary) / name
            with tarfile.open(archive_path, "w:gz", compresslevel=1) as archive:
                for directory in ("lib", "bin", "include"):
                    source = self.venv / directory
                    if source.exists():
                        def exclude_interpreter(info):
                            if info.name.startswith("bin/python"):
                                return None
                            if info.issym() and (self.venv / info.name).resolve() == Path(sys.executable).resolve():
                                return None
                            if info.issym() and Path(info.linkname).is_absolute():
                                raise RuntimeError("Environment contains an external symlink: " + info.name)
                            if "__pycache__" in Path(info.name).parts:
                                return None
                            return info
                        archive.add(source, arcname=directory, filter=exclude_interpreter)
            expected = sha256(archive_path)
            destination = self.snapshots / name
            with archive_path.open("rb") as source, destination.open("xb") as target:
                shutil.copyfileobj(source, target, length=8 * 1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            if sha256(destination) != expected:
                raise RuntimeError("Drive snapshot readback failed; previous snapshot remains current.")
        snapshot = {"schema": 1, "archive": name, "sha256": expected,
                    "created_utc": stamp, "runtime": self.signature,
                    "requirements": requirements, "local_path": str(self.venv)}
        write_json(self.snapshots / (stamp + ".json"), snapshot)
        write_json(self.env_dir / "latest.json", snapshot)
        print("Environment saved and checksum verified:", name)
        return snapshot

    def rebuild(self):
        """Explicitly reinstall recorded packages when Colab's base image changed."""
        if self.activated:
            raise RuntimeError("Restart the kernel before rebuilding an activated environment.")
        previous = json.loads(self.latest.read_text())
        if self.venv.exists():
            raise RuntimeError("Use a fresh runtime for rebuilding; current environment is preserved.")
        self.prepare(restore=False)
        lock = self.local / "requirements.txt"
        lock.write_text(previous["requirements"])
        if previous["requirements"].strip():
            subprocess.run([str(self.python), "-m", "pip", "install", "-r", str(lock)], check=True)
        return self.save_environment()

    def activate(self):
        """Expose this environment to fresh imports in ordinary notebook cells."""
        paths = json.loads(subprocess.check_output(
            [str(self.python), "-c", "import site,json; print(json.dumps(site.getsitepackages()))"], text=True))
        own = [p for p in paths if Path(p).is_relative_to(self.venv)]
        for path in reversed(own):
            site.addsitedir(path)
            sys.path.remove(path)
            sys.path.insert(0, path)
        os.environ["PATH"] = str(self.venv / "bin") + os.pathsep + os.environ["PATH"]
        self.activated = True
        importlib.invalidate_caches()
        os.chdir(self.root)
        print("Notebook imports use the saved environment. Install through ws.install(...).")
        return self

    def write_checkpoint(self, state, name="session.json"):
        """Save explicit JSON state; arbitrary RAM/GPU state is never implicit."""
        if Path(name).name != name or not name.endswith(".json"):
            raise ValueError("Checkpoint name must be a simple .json filename.")
        write_json(self.root / "checkpoints" / name, state)
        return self.root / "checkpoints" / name

    def read_checkpoint(self, name="session.json"):
        if Path(name).name != name or not name.endswith(".json"):
            raise ValueError("Checkpoint name must be a simple .json filename.")
        path = self.root / "checkpoints" / name
        return json.loads(path.read_text()) if path.exists() else None

    def flush(self):
        """Flush Drive writes before the user releases the VM."""
        from google.colab import drive
        drive.flush_and_unmount()
        print("Drive writes flushed. You may now disconnect and delete this runtime.")
