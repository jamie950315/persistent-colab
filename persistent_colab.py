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
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
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
        "base_packages": [list(item) for item in normalized],
    }


PLATFORM_KEYS = ("python", "machine", "os", "libc")


def package_drift(saved, current):
    """Describe base package differences as sorted 'name: old -> new' lines."""
    before, after = dict(map(tuple, saved)), dict(map(tuple, current))
    return [f"{name}: {before.get(name, 'absent')} -> {after.get(name, 'absent')}"
            for name in sorted(before.keys() | after.keys()) if before.get(name) != after.get(name)]


LOCK_HEARTBEAT = 60
LOCK_STALE = 600


def _utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lock_age(record):
    heartbeat = datetime.strptime(record["heartbeat_utc"], "%Y-%m-%dT%H:%M:%SZ")
    return (datetime.now(timezone.utc) - heartbeat.replace(tzinfo=timezone.utc)).total_seconds()


def _heartbeat_name(path):
    return "colab-workspace-lock:" + str(path)


def _start_heartbeat(path):
    name = _heartbeat_name(path)
    if any(thread.name == name and thread.is_alive() for thread in threading.enumerate()):
        return
    stop = threading.Event()

    def beat():
        while not stop.wait(LOCK_HEARTBEAT):
            try:
                record = json.loads(path.read_text())
                if record.get("host") != socket.gethostname():
                    return
                record.update(heartbeat_utc=_utc(), pid=os.getpid())
                write_json(path, record)
            except (OSError, ValueError):
                # Storage was unmounted or the lock was removed; never recreate it.
                return

    thread = threading.Thread(target=beat, name=name, daemon=True)
    thread.stop = stop
    thread.start()


def acquire_lock(root, session=None, force=False):
    """Claim a workspace for this runtime; detects, but cannot prevent, racing writers."""
    path = Path(root) / "environment" / "LOCK"
    host = socket.gethostname()
    record = {"session": session, "host": host, "pid": os.getpid(),
              "created_utc": _utc(), "heartbeat_utc": _utc()}
    try:
        with path.open("xb") as stream:
            stream.write(json.dumps(record, indent=2).encode() + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        try:
            current = json.loads(path.read_text())
        except (OSError, ValueError):
            current = None
        if not force and (current is None or current.get("host") != host):
            if current is None:
                raise RuntimeError(f"Unreadable workspace lock {path}. Retry with --force-unlock "
                                   "(notebook: ws.prepare(force_unlock=True)) if no runtime uses it.") from None
            age = _lock_age(current)
            owner = f"session {current.get('session')!r} on host {current.get('host')!r}"
            if age > LOCK_STALE:
                raise RuntimeError(f"Workspace lock held by {owner} is stale (no heartbeat for {age / 60:.0f} min). "
                                   "If that runtime is gone, retry with --force-unlock "
                                   "(notebook: ws.prepare(force_unlock=True)).") from None
            raise RuntimeError(f"Workspace is in use by {owner} (heartbeat {age:.0f}s ago). "
                               "Use one writer per project; finish or flush the other runtime first.") from None
        if current and current.get("host") == host:
            record["created_utc"] = current.get("created_utc", record["created_utc"])
        write_json(path, record)
    _start_heartbeat(path)
    return path


def lock_held_elsewhere(root):
    path = Path(root) / "environment" / "LOCK"
    try:
        return json.loads(path.read_text()).get("host") != socket.gethostname()
    except FileNotFoundError:
        return False
    except ValueError:
        return True


def release_lock(root):
    """Stop the heartbeat and remove this runtime's lock before storage is flushed."""
    path = Path(root) / "environment" / "LOCK"
    for thread in threading.enumerate():
        if thread.name == _heartbeat_name(path):
            thread.stop.set()
            thread.join()
    if path.exists() and not lock_held_elsewhere(root):
        path.unlink()


def prune_snapshots(root, keep, apply=False):
    """Remove old snapshot pairs, always keeping the newest ones and the latest pointer."""
    if keep < 1:
        raise ValueError("keep must be at least 1.")
    snapshots = Path(root) / "environment" / "snapshots"
    latest = Path(root) / "environment" / "latest.json"
    current = json.loads(latest.read_text())["created_utc"] if latest.exists() else None
    stamps = sorted({path.name.split(".", 1)[0] for path in snapshots.iterdir()
                     if path.name.endswith((".tar.gz", ".json"))}, reverse=True)
    retained = set(stamps[:keep]) | {current}
    removed = [stamp for stamp in stamps if stamp not in retained]
    for stamp in removed:
        for path in (snapshots / (stamp + ".tar.gz"), snapshots / (stamp + ".json")):
            if path.exists():
                print(("Deleted " if apply else "Would delete ") + str(path))
                if apply:
                    path.unlink()
    print(f"Kept {len(stamps) - len(removed)} snapshot(s); {'deleted' if apply else 'would delete'} {len(removed)}.")
    if removed and not apply:
        print("Dry run only. Repeat with --yes to delete.")
    return removed


RCLONE_CONF = Path("/root/.config/rclone/rclone.conf")
RCLONE_STATE = Path("/root/.config/colab-workspace/rclone-mount.json")
RCLONE_RC = "127.0.0.1:5572"
RCLONE_LOG = Path("/tmp/colab-workspace-rclone.log")
# Colab ships FUSE 2 only; rclone execs fusermount3, which accepts the same protocol.
FUSE_SHIM = Path("/opt/colab-workspace/bin")


def _rclone(*arguments, config=RCLONE_CONF):
    return subprocess.run(["rclone", "--config", str(config), *map(str, arguments)],
                          check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL)


def _remote_join(remote, relative):
    relative = str(relative).strip("/")
    if not relative or relative == ".":
        return remote
    return remote + relative if remote.endswith(":") else remote + "/" + relative


def rclone_state():
    return json.loads(RCLONE_STATE.read_text()) if RCLONE_STATE.exists() else None


def _install_rclone():
    if shutil.which("rclone"):
        return
    script = subprocess.run("curl -fsSL https://rclone.org/install.sh | bash", shell=True,
                            capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if script.returncode and not shutil.which("rclone"):
        apt = subprocess.run("apt-get update -qq && apt-get install -y -qq rclone", shell=True,
                             capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if apt.returncode:
            raise RuntimeError("Could not install rclone:\n" + script.stderr[-1000:] + apt.stderr[-1000:])


def _fuse_environment():
    environment = os.environ.copy()
    if not shutil.which("fusermount3"):
        fusermount = shutil.which("fusermount")
        if fusermount is None or not Path("/dev/fuse").exists():
            raise RuntimeError("FUSE is unavailable on this runtime (/dev/fuse or fusermount missing).")
        FUSE_SHIM.mkdir(parents=True, exist_ok=True)
        shim = FUSE_SHIM / "fusermount3"
        if not shim.is_symlink():
            shim.symlink_to(fusermount)
        environment["PATH"] = str(FUSE_SHIM) + os.pathsep + environment["PATH"]
    return environment


def mount_rclone(remote, mount_point, staged_config, timeout=120):
    """Install a staged rclone config and mount the remote; remove the config on failure."""
    mount_point = Path(mount_point).resolve()
    state = rclone_state()
    if os.path.ismount(mount_point):
        Path(staged_config).unlink(missing_ok=True)
        if state and state["remote"] == remote and state["mount_point"] == str(mount_point):
            print("rclone remote is already mounted:", mount_point)
            return state
        raise RuntimeError(f"{mount_point} is already mounted by something else.")
    try:
        RCLONE_CONF.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(RCLONE_CONF.parent, 0o700)
        shutil.move(staged_config, RCLONE_CONF)
        os.chmod(RCLONE_CONF, 0o600)
        _install_rclone()
        environment = _fuse_environment()
        mount_point.mkdir(parents=True, exist_ok=True)
        if any(mount_point.iterdir()):
            raise RuntimeError(f"{mount_point} is not empty; choose an empty mount point.")
        # A new session keeps the daemon alive when the kernel restarts.
        daemon = subprocess.run(
            ["rclone", "--config", str(RCLONE_CONF), "mount", remote, str(mount_point),
             "--vfs-cache-mode", "writes", "--rc", "--rc-addr", RCLONE_RC, "--rc-no-auth",
             "--daemon", "--log-file", str(RCLONE_LOG), "--log-level", "NOTICE"],
            env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + timeout
        while not os.path.ismount(mount_point):
            if daemon.returncode or time.monotonic() > deadline:
                log = RCLONE_LOG.read_text()[-2000:] if RCLONE_LOG.exists() else ""
                raise RuntimeError("rclone mount did not become ready:\n" + log)
            time.sleep(1)
        os.listdir(mount_point)
        state = {"remote": remote, "mount_point": str(mount_point),
                 "config": str(RCLONE_CONF), "rc": RCLONE_RC}
        RCLONE_STATE.parent.mkdir(parents=True, exist_ok=True)
        write_json(RCLONE_STATE, state)
        print("Mounted rclone remote at", mount_point)
        return state
    except BaseException:
        Path(staged_config).unlink(missing_ok=True)
        RCLONE_CONF.unlink(missing_ok=True)
        raise


def _md5(path):
    digest = hashlib.md5()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def flush_rclone(workspace_roots=(), timeout=1800, poll=2):
    """Wait for VFS uploads, verify snapshot pointers remotely, unmount and delete the config."""
    state = rclone_state()
    if state is None:
        raise RuntimeError("No rclone mount was created by colab-workspace on this runtime.")
    mount_point = Path(state["mount_point"]).resolve()
    config = Path(state["config"])
    url = "http://" + state["rc"] + "/"
    deadline = time.monotonic() + timeout
    while True:
        cache = json.loads(_rclone("rc", "vfs/stats", "--url", url, config=config).stdout)["diskCache"]
        if cache.get("erroredFiles"):
            raise RuntimeError(f"rclone reports {cache['erroredFiles']} file(s) that failed to upload; see {RCLONE_LOG}.")
        if cache["uploadsInProgress"] == 0 and cache["uploadsQueued"] == 0:
            break
        if time.monotonic() > deadline:
            raise RuntimeError("Timed out waiting for rclone uploads; the remote is still mounted.")
        time.sleep(poll)
    for root in map(Path, workspace_roots):
        latest = root / "environment" / "latest.json"
        if not latest.exists():
            continue
        remote_root = _remote_join(state["remote"], root.relative_to(mount_point))
        local_pointer = json.loads(latest.read_text())
        remote_pointer = json.loads(_rclone(
            "cat", _remote_join(remote_root, "environment/latest.json"), config=config).stdout)
        if remote_pointer != local_pointer:
            raise RuntimeError("Remote latest.json differs from the local copy; the remote is still mounted.")
        archive = root / "environment" / "snapshots" / local_pointer["archive"]
        listing = json.loads(_rclone(
            "lsjson", "--stat", "--hash", "--hash-type", "md5",
            _remote_join(remote_root, "environment/snapshots/" + local_pointer["archive"]),
            config=config).stdout)
        remote_md5 = (listing.get("Hashes") or {}).get("md5")
        if listing.get("Size") != archive.stat().st_size or (remote_md5 and remote_md5 != _md5(archive)):
            raise RuntimeError("Remote snapshot archive does not match; the remote is still mounted.")
        print("Verified remote snapshot:", local_pointer["archive"])
    if Path.cwd().is_relative_to(mount_point):
        os.chdir("/")
    fusermount = shutil.which("fusermount3") or shutil.which("fusermount")
    if fusermount is None:
        raise RuntimeError("fusermount is unavailable; the remote is still mounted.")
    subprocess.run([fusermount, "-u", str(mount_point)], check=True,
                   capture_output=True, text=True, stdin=subprocess.DEVNULL)
    while os.path.ismount(mount_point):
        if time.monotonic() > deadline:
            raise RuntimeError("Timed out unmounting the rclone remote.")
        time.sleep(poll)
    # rclone may rewrite the config when refreshing a token; remove every copy.
    for path in config.parent.glob(config.name + "*"):
        path.unlink()
    RCLONE_STATE.unlink()
    print("rclone uploads verified; remote unmounted and credentials removed.")


class Workspace:
    """Use durable files on Drive and executable packages on the current VM."""

    def __init__(self, project="default", drive_root=None, local_root=None, session=None):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", project):
            raise ValueError("Project must use letters, digits, underscores or hyphens.")
        default_drive = Path("/content/drive/MyDrive")
        if drive_root is None and not default_drive.is_dir():
            raise RuntimeError("Mount Google Drive before creating a workspace.")
        self.project = project
        self.session = session
        # Subprocesses run with cwd=self.root, so relative paths must be absolute.
        self.root = (Path(drive_root or default_drive / "PersistentColab" / "workspaces") / project).resolve()
        self.local = (Path(local_root or "/content/persistent-colab") / project).resolve()
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

    def prepare(self, restore=True, force_unlock=False, allow_base_drift=False):
        """Claim the workspace, recreate executable paths and restore the latest compatible snapshot."""
        acquire_lock(self.root, self.session, force_unlock)
        try:
            return self._prepare(restore, allow_base_drift)
        except BaseException:
            release_lock(self.root)
            raise

    def _compatibility(self, saved, allow_base_drift):
        """Return base package drift accepted for this restore, or raise."""
        rebuild = ("Review environment/latest.json, then explicitly run ws.rebuild() to "
                   "reinstall the saved requirements on this image. Old snapshots are kept.")
        if any(saved.get(key) != self.signature.get(key) for key in PLATFORM_KEYS):
            raise RuntimeError("The Colab Python/base image changed (Python, architecture or libc). "
                               "The saved environment was not loaded. " + rebuild)
        if saved.get("base_packages_sha256") == self.signature.get("base_packages_sha256"):
            return []
        if "base_packages" not in saved:
            raise RuntimeError("The Colab Python/base image changed. The saved environment was not loaded. " + rebuild)
        drift = package_drift(saved["base_packages"], self.signature["base_packages"])
        if not allow_base_drift:
            raise RuntimeError("The Colab base image changed; the saved environment was not loaded. "
                               "Changed base packages:\n  " + "\n  ".join(drift) +
                               "\nRetry with allow_base_drift=True (CLI: --allow-base-drift) to restore and "
                               "validate it with pip check, or " + rebuild)
        print("Base packages differ from the snapshot:\n  " + "\n  ".join(drift))
        return drift

    def _dependency_problems(self, python):
        result = subprocess.run([str(python), "-m", "pip", "check"], capture_output=True, text=True)
        return set(result.stdout.splitlines()) if result.returncode else set()

    def _prepare(self, restore, allow_base_drift):
        ready = self.local / "environment-ready.json"
        if self.python.exists():
            if ready.exists() and json.loads(ready.read_text()) == self.signature:
                return self
            raise RuntimeError("Incomplete local environment. Start a fresh runtime to restore safely.")
        snapshot = json.loads(self.latest.read_text()) if restore and self.latest.exists() else None
        drift = self._compatibility(snapshot["runtime"], allow_base_drift) if snapshot else []
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
            if drift:
                # Colab images already ship some unmet requirements; only new ones fail.
                problems = self._dependency_problems(self.python) - self._dependency_problems(sys.executable)
                if problems:
                    shutil.rmtree(self.venv)
                    raise RuntimeError("Restored environment conflicts with the new base image; it was removed:\n  "
                                       + "\n  ".join(sorted(problems)))
                print("pip check found no new conflicts with the changed base image.")
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
        if lock_held_elsewhere(self.root):
            raise RuntimeError("Another runtime holds this workspace's lock; the environment was not saved.")
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
        snapshot = {"schema": 2, "archive": name, "sha256": expected,
                    "created_utc": stamp, "runtime": self.signature,
                    "requirements": requirements, "local_path": str(self.venv)}
        write_json(self.snapshots / (stamp + ".json"), snapshot)
        write_json(self.env_dir / "latest.json", snapshot)
        print("Environment saved and checksum verified:", name)
        return snapshot

    def rebuild(self, force_unlock=False):
        """Explicitly reinstall recorded packages when Colab's base image changed."""
        if self.activated:
            raise RuntimeError("Restart the kernel before rebuilding an activated environment.")
        previous = json.loads(self.latest.read_text())
        if self.venv.exists():
            raise RuntimeError("Use a fresh runtime for rebuilding; current environment is preserved.")
        self.prepare(restore=False, force_unlock=force_unlock)
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
        """Flush storage writes before the user releases the VM."""
        release_lock(self.root)
        state = rclone_state()
        if state and self.root.is_relative_to(Path(state["mount_point"]).resolve()):
            flush_rclone([self.root])
        else:
            from google.colab import drive
            drive.flush_and_unmount()
        print("Storage writes flushed. You may now disconnect and delete this runtime.")
