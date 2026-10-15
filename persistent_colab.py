"""Restore a Python workspace from Drive without pretending the VM is persistent."""
from __future__ import annotations

import hashlib
import gzip
import importlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import site
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager


SYSTEM_ROOTS = ("/usr", "/opt", "/etc", "/var/lib", "/root/.local")
SYSTEM_EXCLUDES = (
    "/proc", "/sys", "/dev", "/run", "/tmp", "/var/tmp", "/mnt", "/media", "/content",
    "/root/.config", "/root/.cache", "/root/.ssh", "/root/.local/share/jupyter",
    "/root/.local/share/keyrings", "/root/.local/share/colab", "/var/log",
    "/var/cache/apt/archives", "/var/lib/apt/lists", "/var/lib/private",
    "/etc/hostname", "/etc/hosts", "/etc/resolv.conf", "/etc/machine-id", "/etc/mtab",
    "/etc/shadow", "/etc/shadow-", "/etc/gshadow", "/etc/gshadow-", "/etc/ssl/private",
    "/etc/security/opasswd", "/var/lib/dbus/machine-id", "/var/lib/dpkg/lock",
    "/var/lib/dpkg/lock-frontend", "/opt/colab-workspace",
    "/usr/bin/rclone", "/usr/local/bin/rclone", "/etc/pip.conf", "/etc/apt/auth.conf",
    "/etc/apt/auth.conf.d",
)


class PhaseTimer:
    """Wall-clock seconds for non-overlapping operation phases."""

    def __init__(self):
        self.started = time.monotonic()
        self.phases = {}

    @contextmanager
    def phase(self, name):
        started = time.monotonic()
        try:
            yield
        finally:
            self.phases[name] = round(time.monotonic() - started, 3)

    def metrics(self):
        return {"seconds": round(time.monotonic() - self.started, 3),
                "phases": dict(self.phases)}


def valid_name(value, label="Project"):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise ValueError(f"{label} must use letters, digits, underscores or hyphens.")
    return value


def container_start():
    """PID 1 start in wall-clock seconds (host boot alone is not the container start)."""
    boot = int(next(line.split()[1] for line in Path("/proc/stat").read_text().splitlines()
                    if line.startswith("btime ")))
    ticks = int(Path("/proc/1/stat").read_text().rsplit(")", 1)[1].split()[19])
    return boot + ticks / os.sysconf("SC_CLK_TCK")


def system_image():
    """Use immutable image markers, never the mutable installed-package list."""
    release = os.environ.get("COLAB_RELEASE_TAG")
    if not release:
        raise RuntimeError("System persistence requires a Colab image release identifier.")
    cache = Path("/content/persistent-colab/system-image.json")
    started = container_start()
    if cache.exists():
        value = json.loads(cache.read_text())
        if value["container_start"] == started:
            return value["id"], value["descriptor"]
    cuda = Path("/usr/local/cuda/version.json")
    descriptor = {"release": release, "python": platform.python_version(),
                  "machine": platform.machine(), "libc": list(platform.libc_ver()),
                  "os_release": Path("/etc/os-release").read_text(),
                  "accelerator_image": bool(os.environ.get("COLAB_GPU", "").strip("0")),
                  "cuda": cuda.read_text() if cuda.exists() else None}
    image_id = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()[:24]
    cache.parent.mkdir(parents=True, exist_ok=True)
    write_json(cache, {"container_start": started, "id": image_id, "descriptor": descriptor})
    return image_id, descriptor


def system_packages():
    apt = subprocess.check_output(["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${db:Status-Status}\n"], text=True)
    apt = dict((fields[0], fields[1]) for line in apt.splitlines()
               if len(fields := line.split("\t")) == 3 and fields[2] == "installed")
    pip = json.loads(subprocess.check_output([sys.executable, "-m", "pip", "list", "--format=json"], text=True))
    return {"apt": apt, "pip": {p["name"].lower().replace("_", "-"): p["version"] for p in pip}}


def _write_gzip_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with gzip.open(temporary, "wt", compresslevel=1) as stream:
        json.dump(value, stream, separators=(",", ":"))
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _read_gzip_json(path):
    with gzip.open(path, "rt") as stream:
        return json.load(stream)


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
        state = rclone_state()
        if state and path.resolve().is_relative_to(Path(state["mount_point"]).resolve()):
            # Deleting a file whose rewrite is still queued drops only the queued
            # upload; the previous object would stay on the remote.
            wait_rclone_uploads(state)
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
    _prune_system_layers(Path(root), keep, apply, retained)
    return removed


def _prune_system_layers(root, keep, apply, retained_snapshots):
    protected = {}
    pointers = list((root / "environment/system").glob("*/latest.json"))
    for stamp in retained_snapshots - {None}:
        path = root / "environment/snapshots" / (stamp + ".json")
        if path.exists():
            snapshot = json.loads(path.read_text())
            if snapshot.get("system"):
                pointer = snapshot["system"]
                protected.setdefault(pointer["image_id"], set()).update(item["id"] for item in pointer["chain"])
    for path in pointers:
        pointer = json.loads(path.read_text())
        protected.setdefault(pointer["image_id"], set()).update(item["id"] for item in pointer["chain"])
    for folder in (root / "environment/system").glob("*/layers"):
        ids = sorted({path.name.split(".", 1)[0] for path in folder.glob("*.json")}, reverse=True)
        retained = protected.get(folder.parent.name, set()) | set(ids[:keep])
        for layer_id in set(ids) - retained:
            for extension in (".json", ".tar.gz"):
                path = folder / (layer_id + extension)
                print(("Deleted " if apply else "Would delete ") + str(path))
                if apply:
                    path.unlink(missing_ok=True)


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


def wait_rclone_uploads(state, timeout=1800, poll=2):
    """Block until the rclone VFS has no queued or running uploads."""
    url = "http://" + state["rc"] + "/"
    deadline = time.monotonic() + timeout
    while True:
        cache = json.loads(_rclone("rc", "vfs/stats", "--url", url, config=state["config"]).stdout)["diskCache"]
        if cache.get("erroredFiles"):
            raise RuntimeError(f"rclone reports {cache['erroredFiles']} file(s) that failed to upload; see {RCLONE_LOG}.")
        if cache["uploadsInProgress"] == 0 and cache["uploadsQueued"] == 0:
            return
        if time.monotonic() > deadline:
            raise RuntimeError("Timed out waiting for rclone uploads; the remote is still mounted.")
        time.sleep(poll)


def flush_rclone(workspace_roots=(), timeout=1800, poll=2):
    """Wait for VFS uploads, verify the remote copy, unmount and delete the config."""
    state = rclone_state()
    if state is None:
        raise RuntimeError("No rclone mount was created by colab-workspace on this runtime.")
    mount_point = Path(state["mount_point"]).resolve()
    config = Path(state["config"])
    deadline = time.monotonic() + timeout
    wait_rclone_uploads(state, timeout, poll)
    for root in map(Path, workspace_roots):
        remote_root = _remote_join(state["remote"], root.relative_to(mount_point))
        remote_files = _rclone("lsf", "--files-only", _remote_join(remote_root, "environment"),
                               config=config).stdout.split("\n")
        if "LOCK" in remote_files:
            remote_lock = json.loads(_rclone("cat", _remote_join(remote_root, "environment/LOCK"),
                                             config=config).stdout)
            if remote_lock.get("host") == socket.gethostname():
                raise RuntimeError("This runtime's lock is still on the remote; the remote is still mounted.")
        _verify_rclone_system(state, root)
        latest = root / "environment" / "latest.json"
        if not latest.exists():
            continue
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


def _verify_rclone_system(state, root):
    """Verify system-only rebuilds too, even before a venv snapshot exists."""
    config = state["config"]
    remote_root = _remote_join(state["remote"], root.relative_to(Path(state["mount_point"])))
    # System layers are part of finish's durability gate, including the image baseline.
    for system_latest in (root / "environment/system").glob("*/latest.json"):
        relative = system_latest.relative_to(root)
        system_pointer = json.loads(system_latest.read_text())
        remote_system = json.loads(_rclone("cat", _remote_join(remote_root, relative), config=config).stdout)
        if remote_system != system_pointer:
            raise RuntimeError("Remote system pointer differs; the remote is still mounted.")
        for reference in system_pointer["chain"]:
            manifest_path = system_latest.parent / "layers" / (reference["id"] + ".json")
            manifest = json.loads(manifest_path.read_text())
            remote_manifest = _rclone("cat", _remote_join(remote_root, manifest_path.relative_to(root)),
                                      config=config).stdout
            if json.loads(remote_manifest) != manifest or sha256(manifest_path) != reference["sha256"]:
                raise RuntimeError("Remote system manifest differs; the remote is still mounted.")
            _verify_rclone_file(state, manifest_path.with_suffix(".tar.gz"))
        data_root = root.parents[3]
        baseline = data_root / "images" / system_pointer["image_id"] / "baseline.json.gz"
        if sha256(baseline) != system_pointer["baseline_sha256"]:
            raise RuntimeError("System baseline differs; the remote is still mounted.")
        _verify_rclone_file(state, baseline)
        _verify_rclone_file(state, baseline.with_name("baseline.json"))
        print("Verified remote system chain:", system_pointer["image_id"], len(system_pointer["chain"]))


def _verify_rclone_file(state, path):
    remote = _remote_join(state["remote"], path.relative_to(Path(state["mount_point"])))
    listing = json.loads(_rclone("lsjson", "--stat", "--hash", "--hash-type", "md5", remote,
                                config=state["config"]).stdout)
    md5 = (listing.get("Hashes") or {}).get("md5")
    if listing.get("Size") != path.stat().st_size or (md5 and md5 != _md5(path)):
        raise RuntimeError("Remote system archive does not match; the remote is still mounted.")


class SystemLayer:
    """Immutable, image-specific filesystem deltas; restore never invokes an installer.

    The filesystem and package inventory are injectable for account-free tests.
    Production callers must initialize on a fresh VM before installing packages.
    """

    def __init__(self, workspace_root, data_root, local, *, fs_root="/", image_id=None,
                 start_time=None, packages=None, mounts=None, owner_path=None):
        self.root = Path(workspace_root)
        self.data_root = Path(data_root)
        self.local = Path(local)
        self.fs = Path(fs_root).resolve()
        self.real = self.fs == Path("/")
        self.image_id, self.image = system_image() if image_id is None else (image_id, {"test_image": image_id})
        valid_name(self.image_id, "Image ID")
        self.start = container_start() if start_time is None else start_time
        self.inventory = packages or system_packages
        self.folder = self.root / "environment" / "system" / self.image_id
        self.layers = self.folder / "layers"
        self.latest = self.folder / "latest.json"
        self.baseline_dir = self.data_root / "images" / self.image_id
        self.baseline_path = self.baseline_dir / "baseline.json.gz"
        self.state_path = self.local / "system-state.json.gz"
        self.owner_path = Path(owner_path or ("/content/persistent-colab/system-owner.json" if self.real
                                              else self.fs / "content/system-owner.json"))
        if mounts is None:
            mounts = [line.split()[4].replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")
                      for line in Path("/proc/self/mountinfo").read_text().splitlines()] if self.real else []
        self.mounts = {p.rstrip("/") for p in mounts if p != "/"}
        self.device = self.fs.stat().st_dev
        self.baseline = None

    def allowed(self, name):
        parts = Path(name).parts
        return (name.startswith("/") and ".." not in parts and "__pycache__" not in parts
                and parts[-1] != "direct_url.json"
                and any(name == root or name.startswith(root + "/") for root in SYSTEM_ROOTS)
                and not any(name == root or name.startswith(root + "/") for root in SYSTEM_EXCLUDES)
                and not name.startswith("/etc/ssh/ssh_host_")
                and not any(name == root or name.startswith(root + "/") for root in self.mounts))

    def path(self, name):
        if not self.allowed(name):
            raise RuntimeError("Excluded or invalid system path: " + name)
        return self.fs / name.lstrip("/")

    @staticmethod
    def fingerprint(info, path):
        return [info.st_ctime_ns, info.st_mtime_ns, info.st_size, info.st_mode,
                info.st_uid, info.st_gid, os.readlink(path) if stat.S_ISLNK(info.st_mode) else None]

    def scan(self):
        """Walk only the root device, never following links or entering bind mounts."""
        result = {}
        def visit(path, name):
            if not self.allowed(name):
                return
            try:
                info = path.lstat()
            except FileNotFoundError:
                return
            if info.st_dev != self.device or not (stat.S_ISREG(info.st_mode) or
                    stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                return
            result[name] = self.fingerprint(info, path)
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as entries:
                    for entry in entries:
                        visit(Path(entry.path), name + "/" + entry.name)
        for root in SYSTEM_ROOTS:
            visit(self.fs / root.lstrip("/"), root)
        return result

    def ensure_baseline(self):
        if self.baseline_path.exists():
            meta = json.loads((self.baseline_dir / "baseline.json").read_text())
            self.baseline_sha256 = sha256(self.baseline_path)
            if self.baseline_sha256 != meta["sha256"]:
                raise RuntimeError("System baseline checksum mismatch.")
            self.baseline = _read_gzip_json(self.baseline_path)
        else:
            # This must happen during prepare on the fresh image, before user installs.
            started = time.monotonic()
            paths = self.scan()
            value = {"schema": 1, "image_id": self.image_id, "image": self.image,
                     "paths": sorted(paths), "packages": self.inventory()}
            self.baseline_dir.mkdir(parents=True, exist_ok=True)
            self.local.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=self.local) as directory:
                source = Path(directory) / "baseline.json.gz"
                _write_gzip_json(source, value)
                self.baseline_sha256 = sha256(source)
                self._copy_verified(source, self.baseline_path, self.baseline_sha256)
            write_json(self.baseline_dir / "baseline.json", {"image_id": self.image_id,
                       "sha256": self.baseline_sha256})
            self.baseline = value
            print(f"System baseline: {len(paths)} paths, {self.baseline_path.stat().st_size} bytes, "
                  f"{time.monotonic() - started:.2f}s; image {self.image_id}.")
        if self.baseline["image_id"] != self.image_id:
            raise RuntimeError("System baseline image ID mismatch.")
        return self.baseline

    @staticmethod
    def _copy_verified(source, destination, expected=None):
        expected = expected or sha256(source)
        with Path(source).open("rb") as incoming, Path(destination).open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=8 * 1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if sha256(destination) != expected:
            raise RuntimeError("System layer readback failed; previous pointer remains current.")

    def _claim(self):
        identity = {"workspace": str(self.root.resolve()), "image_id": self.image_id}
        self.owner_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.owner_path.open("x") as stream:
                json.dump(identity, stream)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            owner = json.loads(self.owner_path.read_text())
            if owner != identity:
                raise RuntimeError("Another project's system layer is already applied on this VM. "
                                   "Use a fresh VM for " + str(self.root) + ".")

    def _load_pointer(self):
        if self.latest.exists():
            pointer = json.loads(self.latest.read_text())
            if pointer.get("image_id") != self.image_id:
                raise RuntimeError("System layer image ID mismatch; no files were restored.")
            return pointer
        alternatives = sorted((self.root / "environment/system").glob("*/latest.json"))
        if alternatives:
            recipes = [json.loads(path.read_text()) for path in alternatives]
            lines = ["System image ID mismatch; no files were restored. Current image: " + self.image_id]
            for recipe in recipes:
                lines.append("Saved image " + recipe["image_id"] + ": " + json.dumps(recipe["packages"], sort_keys=True))
            raise RuntimeError("\n".join(lines) + "\nUse a fresh VM and explicitly run ws.rebuild_system() "
                               "(CLI: rebuild-system) to reinstall recorded apt/system pip packages.")
        return None

    def _chain(self, pointer):
        if pointer.get("image_id") != self.image_id:
            raise RuntimeError("System layer image ID mismatch; no files were restored.")
        result = []
        seen = set()
        for reference in pointer["chain"]:
            layer_id = valid_name(reference["id"], "Layer ID")
            if layer_id in seen:
                raise RuntimeError("Repeated system layer in chain.")
            seen.add(layer_id)
            path = self.layers / (layer_id + ".json")
            contents = path.read_bytes()
            if hashlib.sha256(contents).hexdigest() != reference["sha256"]:
                raise RuntimeError("System manifest checksum mismatch; no files were restored.")
            manifest = json.loads(contents)
            if manifest["image_id"] != self.image_id or manifest["id"] != layer_id:
                raise RuntimeError("System manifest image ID mismatch; no files were restored.")
            if manifest.get("parent") != (result[-1][0]["id"] if result else None):
                raise RuntimeError("Broken system layer chain.")
            result.append((manifest, self.layers / (layer_id + ".tar.gz")))
        return result

    def _safe_destination(self, name):
        target = self.path(name)
        current = target.parent
        while current != self.fs:
            if current.is_symlink():
                raise RuntimeError("System archive crosses a symlink parent: " + name)
            current = current.parent
        return target

    def _validate_archive(self, archive, manifest):
        names = set()
        links = set()
        for member in archive.getmembers():
            name = "/" + member.name
            if member.name.startswith("/") or name not in manifest["files"] or not self.allowed(name):
                raise RuntimeError("Invalid system archive member: " + member.name)
            if name in names or not (member.isfile() or member.isdir() or member.issym()):
                raise RuntimeError("Unsupported system archive member: " + member.name)
            names.add(name)
            if member.issym():
                # Preserve user-space links, including absolute /etc/alternatives links.
                # They are never followed while extracting other members.
                links.add(name)
            self._safe_destination(name)
        if names != set(manifest["files"]):
            raise RuntimeError("System archive and manifest paths differ.")
        if any(any(parent in links for parent in map(str, Path(name).parents)) for name in names):
            raise RuntimeError("System archive contains a symlink parent.")
        for name in manifest["deleted"]:
            self._safe_destination(name)

    def _apply(self, archive, manifest):
        # Delete deepest paths first. rmdir never removes an unrecorded child.
        for name in sorted(manifest["deleted"], key=lambda p: (p.count("/"), p), reverse=True):
            path = self._safe_destination(name)
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                path.rmdir()
        for member in archive.getmembers():
            path = self._safe_destination("/" + member.name)
            if path.is_symlink() or (path.exists() and not member.isdir()):
                if path.is_dir() and not path.is_symlink():
                    path.rmdir()
                else:
                    path.unlink()
            elif path.exists() and member.isdir() and not path.is_dir():
                path.unlink()
            # All names, types and symlink parents were validated before any writes.
            archive.extract(member, path=self.fs, filter="fully_trusted")

    def _restored_paths(self, chain):
        """Derive image paths and stat only paths written by the verified chain.

        None denotes an untouched image path. On the next full save scan, its
        ctime must precede the restore checkpoint to remain unchanged. ctime
        detects content, metadata and link changes even if mtime is preserved.
        Unknown runtime paths are new, and missing image paths are deletions.
        """
        excluded = re.compile(r"^(?:" + "|".join(map(re.escape, SYSTEM_EXCLUDES + tuple(self.mounts))) + r")(?:/|$)")
        paths = dict.fromkeys(name for name in self.baseline["paths"] if not excluded.match(name))
        written = set()
        for manifest, _ in chain:
            for name in manifest["deleted"]:
                paths.pop(name, None)
            written.difference_update(manifest["deleted"])
            written.update(manifest["files"])
        for name in written:
            path = self._safe_destination(name)
            info = path.lstat()
            paths[name] = self.fingerprint(info, path)
        return paths

    def restore(self):
        timer = PhaseTimer()
        if (self.local / "system-restore-failed.json").exists():
            raise RuntimeError("System restore failed on this VM. Start a fresh VM before restoring again.")
        with timer.phase("pointer_claim"):
            pointer = self._load_pointer()
            self._claim()
        with timer.phase("baseline"):
            self.ensure_baseline()
            if pointer and pointer["baseline_sha256"] != self.baseline_sha256:
                raise RuntimeError("System baseline does not match the saved chain; no files were restored.")
        if self.state_path.exists():
            with timer.phase("local_state"):
                state = _read_gzip_json(self.state_path)
            if state["image_id"] == self.image_id and state.get("chain") == (pointer or {}).get("chain", []):
                write_json(self.local / "system-restore.json", {"image_id": self.image_id,
                           "layers": 0, "bytes": 0, "already_applied": True, **timer.metrics()})
                return pointer
            raise RuntimeError("Local system state differs from the saved chain. Use a fresh VM.")
        size = 0
        with timer.phase("manifests"):
            chain = self._chain(pointer) if pointer else []
        # Validate the complete chain and copy each archive locally before touching /usr.
        self.local.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.local) as directory:
            pending = []
            with timer.phase("copy_verify_validate"):
                for manifest, source in chain:
                    target = Path(directory) / source.name
                    shutil.copyfile(source, target)
                    if sha256(target) != manifest["sha256"]:
                        raise RuntimeError("System layer checksum mismatch; no files were restored.")
                    with tarfile.open(target, "r:gz") as archive:
                        self._validate_archive(archive, manifest)
                    size += target.stat().st_size
                    pending.append((manifest, target))
            try:
                with timer.phase("apply"):
                    for manifest, path in pending:
                        with tarfile.open(path, "r:gz") as archive:
                            self._apply(archive, manifest)
                with timer.phase("ldconfig"):
                    if chain and self.real:
                        subprocess.run(["ldconfig"], check=True, capture_output=True)
            except BaseException:
                # A partial root-filesystem restore cannot be rolled back safely in place.
                write_json(self.local / "system-restore-failed.json", {"image_id": self.image_id})
                raise
        # Sample the filesystem clock, which can be coarser than time.time_ns().
        # Save includes equal timestamps so writes in the same clock tick count.
        with tempfile.TemporaryFile(dir=self.local) as checkpoint:
            baseline_before_ns = os.fstat(checkpoint.fileno()).st_ctime_ns
        with timer.phase("state_derive"):
            paths = self._restored_paths(chain)
        with timer.phase("state_write"):
            self._write_state(paths, pointer, baseline_before_ns=baseline_before_ns)
        metrics = timer.metrics()
        duration = metrics["seconds"]
        write_json(self.local / "system-restore.json", {"image_id": self.image_id,
                   "layers": len(chain), "bytes": size, **metrics})
        print(f"System restored: {len(chain)} layer(s), {size} compressed bytes, "
              f"{duration:.2f}s; image {self.image_id}.")
        print("System restore phases (seconds):", json.dumps(metrics["phases"], sort_keys=True))
        return pointer

    def _write_state(self, paths, pointer, baseline_before_ns=None, destination=None):
        _write_gzip_json(destination or self.state_path, {"image_id": self.image_id,
                         "chain": (pointer or {}).get("chain", []), "paths": paths,
                         "baseline_before_ns": baseline_before_ns})

    def _package_changes(self):
        current = self.inventory()
        return {kind: {name: version for name, version in current[kind].items()
                       if self.baseline["packages"][kind].get(name) != version} for kind in ("apt", "pip")}

    def save(self, compact=False):
        timer = PhaseTimer()
        with timer.phase("baseline_claim"):
            self._claim()
            self.ensure_baseline()
        if (self.local / "system-restore-failed.json").exists():
            raise RuntimeError("System restore failed on this VM. Use a fresh VM; saving is disabled.")
        if not self.state_path.exists():
            raise RuntimeError("Prepare system persistence on a fresh VM before saving.")
        with timer.phase("previous_state"):
            previous = _read_gzip_json(self.state_path)
            pointer = None if getattr(self, "migrating", False) and not self.latest.exists() else self._load_pointer()
            if previous.get("chain") != (pointer or {}).get("chain", []):
                raise RuntimeError("System chain changed since restore; refusing to overwrite it.")
        with timer.phase("scan"):
            current = self.scan()
        if compact:
            effective = set()
            for manifest, _ in self._chain(pointer) if pointer else []:
                effective.difference_update(manifest["deleted"])
                effective.update(manifest["files"])
            changed = {name for name, info in current.items()
                       if name in effective or info[0] > int(self.start * 1e9)}
            deleted = set(self.baseline["paths"]) - current.keys()
            references = []
        else:
            def differs(name, info):
                if name in previous["paths"] and previous["paths"][name] is None:
                    return info[0] >= previous["baseline_before_ns"]
                return previous["paths"].get(name) != info
            changed = {name for name, info in current.items() if (
                differs(name, info) and (pointer is not None or info[0] > int(self.start * 1e9)))}
            # Initial save includes runtime-created system files even if prepare saw them.
            if pointer is None:
                changed.update(name for name, info in current.items() if info[0] > int(self.start * 1e9))
            deleted = ((set(previous["paths"]) if pointer else set(self.baseline["paths"])) - current.keys())
            references = (pointer or {}).get("chain", [])
            if pointer and previous.get("baseline_before_ns") is not None:
                for name in deleted:
                    for ancestor in map(str, Path(name).parents):
                        info = current.get(ancestor)
                        if info is None:
                            continue
                        if (not stat.S_ISDIR(info[3]) and ancestor in previous["paths"]
                                and previous["paths"][ancestor] is None
                                and info[0] < previous["baseline_before_ns"]):
                            raise RuntimeError("System topology changed before restore: " + ancestor +
                                               ". Start a fresh VM before restoring and saving.")
                        break
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
        self.layers.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.local) as directory:
            source = Path(directory) / (stamp + ".tar.gz")
            with timer.phase("archive"):
                # pigz uses multiple CPUs when present; gzip is the Python stdlib implementation.
                if self.real and shutil.which("pigz"):
                    with source.open("wb") as output:
                        compressor = subprocess.Popen(["pigz", "-1", "-p", "2"], stdin=subprocess.PIPE, stdout=output)
                        try:
                            with tarfile.open(fileobj=compressor.stdin, mode="w|") as archive:
                                self._archive_files(archive, changed, current)
                        finally:
                            compressor.stdin.close()
                            code = compressor.wait()
                        if code:
                            raise RuntimeError("System archive compression failed.")
                else:
                    with tarfile.open(source, "w:gz", compresslevel=1) as archive:
                        self._archive_files(archive, changed, current)
            with timer.phase("packages"):
                packages = self._package_changes()
            manifest = {"schema": 1, "id": stamp, "image_id": self.image_id,
                        "parent": references[-1]["id"] if references else None,
                        "sha256": sha256(source), "bytes": source.stat().st_size,
                        "files": sorted(changed), "deleted": sorted(deleted),
                        "packages": packages, "created_utc": _utc()}
            with timer.phase("copy_verify"):
                self._copy_verified(source, self.layers / source.name, manifest["sha256"])
        manifest_path = self.layers / (stamp + ".json")
        write_json(manifest_path, manifest)
        # Read the manifest back too; publish only after every immutable object is verified.
        if json.loads(manifest_path.read_text()) != manifest:
            raise RuntimeError("System manifest readback failed; previous pointer remains current.")
        result = {"schema": 1, "image_id": self.image_id,
                  "baseline_sha256": self.baseline_sha256, "created_utc": manifest["created_utc"],
                  "chain": references + [{"id": stamp, "sha256": sha256(manifest_path)}],
                  "packages": manifest["packages"], "bytes": manifest["bytes"],
                  **timer.metrics()}
        staged = self.local / ("system-state-" + uuid.uuid4().hex + ".json.gz")
        try:
            with timer.phase("state_write"):
                self._write_state(current, result, destination=staged)
            result.update(timer.metrics())
            write_json(self.latest, result)
            os.replace(staged, self.state_path)
        finally:
            staged.unlink(missing_ok=True)
        write_json(self.local / "system-save.json", result)
        print(f"System saved: {len(changed)} paths, {len(deleted)} deletions, {manifest['bytes']} "
              f"compressed bytes, {result['seconds']:.2f}s; {len(result['chain'])} layer(s).")
        print("System save phases (seconds):", json.dumps(result["phases"], sort_keys=True))
        return result

    def _archive_files(self, archive, names, current):
        for name in sorted(names):
            path = self.path(name)
            before = self.fingerprint(path.lstat(), path)
            if before != current[name]:
                raise RuntimeError("System file changed during save: " + name)
            archive.inodes.clear()  # Store hardlinked files independently; never follow symlinks.
            archive.add(path, arcname=name.lstrip("/"), recursive=False)
            if self.fingerprint(path.lstat(), path) != before:
                raise RuntimeError("System file changed during save: " + name)

    def rebuild(self):
        """Explicit migration: installers are allowed only through this method."""
        if self.owner_path.exists() or self.state_path.exists():
            raise RuntimeError("Use a fresh VM for rebuild-system.")
        alternatives = sorted((self.root / "environment/system").glob("*/latest.json"),
                              key=lambda p: json.loads(p.read_text())["created_utc"])
        if not alternatives:
            raise RuntimeError("No recorded system packages to rebuild.")
        recipe = json.loads(alternatives[-1].read_text())["packages"]
        self._claim()
        self.ensure_baseline()
        self._write_state(self.scan(), None)
        self.migrating = True
        if recipe["apt"]:
            subprocess.run(["apt-get", "update", "-qq"], check=True)
            subprocess.run(["apt-get", "install", "-y", *[name + "=" + version
                            for name, version in sorted(recipe["apt"].items())]], check=True)
        if recipe["pip"]:
            subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps",
                            *[name + "==" + version for name, version in sorted(recipe["pip"].items())]], check=True)
        return self.save()


class Workspace:
    """Use durable files on Drive and executable packages on the current VM."""

    def __init__(self, project="default", drive_root=None, local_root=None, session=None,
                 space="default", system=None):
        valid_name(project)
        valid_name(space, "Space")
        default_drive = Path("/content/drive/MyDrive")
        if drive_root is None and not default_drive.is_dir():
            raise RuntimeError("Mount Google Drive before creating a workspace.")
        self.project = project
        self.space = space
        self.session = session
        # Subprocesses run with cwd=self.root, so relative paths must be absolute.
        self.data_root = Path(drive_root or default_drive / "PersistentColabData").resolve()
        self.root = self.data_root / "spaces" / space / "workspaces" / project
        self.local = (Path(local_root or "/content/persistent-colab") / space / project).resolve()
        self.venv = self.local / ".venv"
        self.python = self.venv / "bin" / "python"
        self.env_dir = self.root / "environment"
        self.snapshots = self.env_dir / "snapshots"
        self.latest = self.env_dir / "latest.json"
        enabled = bool(os.environ.get("COLAB_RELEASE_TAG")) if system is None else system
        timer = PhaseTimer()
        with timer.phase("runtime_signature"):
            self.signature = None if enabled else runtime_signature()
        self.initialization_metrics = timer.metrics()
        self.activated = False
        for folder in ("src", "data", "models", "outputs", "checkpoints", "environment/snapshots"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        self.local.mkdir(parents=True, exist_ok=True)
        self.system = SystemLayer(self.root, self.data_root, self.local) if enabled else None
        # Model files are durable, tokens remain in the ephemeral runtime.
        os.environ["HF_HUB_CACHE"] = str(self.root / "models" / "huggingface")
        os.environ["HF_TOKEN_PATH"] = str(self.local / "hf-token")
        os.environ["TORCH_HOME"] = str(self.root / "models" / "torch")

    def prepare(self, restore=True, force_unlock=False, allow_base_drift=False):
        """Claim the workspace, recreate executable paths and restore the latest compatible snapshot."""
        timer = PhaseTimer()
        with timer.phase("lock"):
            acquire_lock(self.root, self.session, force_unlock)
        try:
            if self.system:
                with timer.phase("system_restore"):
                    self.system.restore()
                with timer.phase("runtime_signature"):
                    self.signature = runtime_signature()
            with timer.phase("venv_prepare"):
                result = self._prepare(restore, allow_base_drift)
            self.prepare_metrics = {**timer.metrics(), "initialization": self.initialization_metrics}
            write_json(self.local / "prepare.json", self.prepare_metrics)
            if self.system:
                path = self.local / "system-restore.json"
                if path.exists():
                    metrics = json.loads(path.read_text())
                    metrics["prepare"] = self.prepare_metrics
                    write_json(path, metrics)
            return result
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
            if self.system and ready.exists() and all(json.loads(ready.read_text()).get(key) ==
                                                      self.signature.get(key) for key in PLATFORM_KEYS):
                # System pip installs on this VM intentionally change inherited packages.
                write_json(ready, self.signature)
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

    def save_environment(self, compact=False):
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
        if self.system:
            self.signature = runtime_signature()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
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
        system_snapshot = self.system.save(compact=compact) if self.system else None
        snapshot = {"schema": 2, "archive": name, "sha256": expected,
                    "created_utc": stamp, "runtime": self.signature,
                    "requirements": requirements, "local_path": str(self.venv)}
        if system_snapshot:
            snapshot["system"] = system_snapshot
        write_json(self.snapshots / (stamp + ".json"), snapshot)
        write_json(self.env_dir / "latest.json", snapshot)
        print("Environment saved and checksum verified:", name)
        return snapshot

    def compact(self):
        """Merge the active system chain; immutable old layers remain until explicit prune."""
        if self.system is None:
            raise RuntimeError("System persistence is only available on a Colab VM.")
        return self.save_environment(compact=True)

    def rebuild_system(self, force_unlock=False):
        """Explicitly reinstall a recorded system recipe on a fresh, different image."""
        if self.system is None:
            raise RuntimeError("System persistence is only available on a Colab VM.")
        acquire_lock(self.root, self.session, force_unlock)
        try:
            result = self.system.rebuild()
            self.signature = runtime_signature()
            return result
        except BaseException:
            release_lock(self.root)
            raise

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
