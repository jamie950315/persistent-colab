# Persistent Colab

A general Python/AI workspace stored in Google Drive. Releasing a managed Colab
runtime releases its VM; reopening the notebook restores durable files and the
last saved Python and system environments into a compatible new runtime.

## Use

1. Open the current `PersistentColab.ipynb` from this checkout in Colab. Use CPU unless the task needs a GPU.
2. On a fresh VM, run **Mount Google Drive**, then **Restore workspace, system and Python environments** before installing packages, changing system files or importing project dependencies.
3. Keep the same `SPACE` and `PROJECT` names to continue the same workspace.
4. Write durable files under `ws.root`. Install packages using `ws.install(...)`.
5. Save application/model checkpoints during long jobs.
6. Run **Save environment**, check **FLUSH_DRIVE**, then run **Flush Drive** when finished.
7. Use **Runtime → Disconnect and delete runtime**. Reopen and run steps 1–2
   next time on the same runtime type (CPU, or the same GPU family). CPU and GPU
   runtimes use different base images; see [Python environment](#python-environment).

```
My Drive/PersistentColabData/
  spaces/default/workspaces/default/
    src/
    data/
    models/
    outputs/
    checkpoints/
    environment/
      latest.json
      snapshots/<timestamp>.tar.gz
      snapshots/<timestamp>.json
      system/<image-id>/latest.json
      system/<image-id>/layers/<layer-id>.{json,tar.gz}
  images/<image-id>/baseline.{json,json.gz}
```

System snapshots and package recipes are also stored with the project. The
private `My Drive/PersistentColab` notebook, helper and README were updated in
place on 2026-10-15, preserving file IDs and sharing. Re-sync those files after
helper/notebook changes and preserve private-output settings. The controller
handles interactive Drive mount consent. Existing data is not migrated or used
as the new data root automatically.

## Use from another notebook

Generate a current self-contained notebook, or copy its mount and restore cells
into another notebook. Run them on a fresh VM before installing packages,
changing system files or importing project dependencies:

```sh
colab-workspace notebook --space default -p default -o OtherNotebook.ipynb
```

The restore cell uses `Workspace(PROJECT, space=SPACE)`. The same space and
project share durable files and saved environments across notebooks. A different
space or project selects separate directories under the same shared data root;
`default` is the default for both names. Names accept 1–64 letters, digits,
underscores or hyphens, starting with a letter or digit.

Initialize each notebook explicitly; this is not an account-wide Colab setting.
Use one project per VM because system restoration changes the whole VM, and one
writer per project. Start a fresh VM to switch projects or spaces after system
restoration. All spaces remain accessible to the same Drive account; separate
directories are not a security boundary.

## Companion CLI and skills

Install the companion alongside the official CLI:

```sh
uv tool install google-colab-cli  # Skip if already installed.
git clone https://github.com/jamie950315/persistent-colab.git
cd persistent-colab
uv tool install --editable .
colab-workspace doctor
```

The companion calls the official `colab` executable; it does not replace its
authentication, session state or runtime management. Global `--auth`, `--config`
and `--client-oauth-config` options are forwarded when provided. Otherwise the
official CLI's defaults remain in effect.

```sh
colab new -s development                    # CPU; use --gpu only when needed.
colab-workspace --storage rclone mount-rclone -s development \
  --conf ~/.config/rclone/colab.conf --remote colab:PersistentColabData
colab-workspace --storage rclone restore -s development -p default
colab-workspace --storage rclone install -s development -p default 'humanize==4.15.0'
colab-workspace --storage rclone run -s development -p default -f job.py -- input.csv
colab-workspace --storage rclone status -s development -p default
colab-workspace --storage rclone spaces -s development
colab-workspace --storage rclone save -s development -p default
colab-workspace --storage rclone finish -s development -p default
```

The documented CLI workflow uses rclone and the same `PersistentColabData`
folder as the notebook. For interactive Drive mounting instead, use
`colab-workspace mount -s development` and `--storage colab-drive` on subsequent
commands (the CLI's default storage flag). Both routes use the identical layout.

Run `restore` on a fresh VM before installing workspace or system packages or
changing system files. It calls `ws.prepare()` to record the unmodified image
baseline before restoring saved state; this keeps saved changes and package
rebuild recipes accurate.

`restore` and other workspace commands claim `environment/LOCK` for the
runtime; `flush` and `finish` release it. A command on another runtime fails
while the lock's heartbeat is fresh. If a runtime disappeared without
`finish`, the lock turns stale after 10 minutes and the error suggests
`--force-unlock`. `prune -s SESSION -p PROJECT --keep N` lists older snapshots to delete
and deletes them only with `--yes`; the newest `N` and the `latest.json`
snapshot are always kept. It also lists unreferenced system layers; layers needed
by the current system chain are retained.

`--space NAME` can precede or follow any subcommand. For example,
`colab-workspace --storage rclone --space research restore -s development -p training` and
`colab-workspace --storage rclone restore -s development --space research -p training` select the
same workspace. `spaces -s SESSION` lists existing spaces and their projects
using the VM's mounted storage; it does not mount, create or allocate anything.

`run` uses an independent workspace Python process, passes script arguments and
its exit code, and sets the durable workspace as cwd. Imports can use `src/` and
the workspace root. Only the specified script is transferred; upload source or
data dependencies separately with official `colab upload`.

`finish` saves both system and Python environments, flushes/unmounts Drive and
releases only the named VM after a structured success receipt. Save application checkpoints
beforehand. A remote Python error, timeout or missing receipt leaves the VM
running for inspection, even when the official `colab exec` exits zero.
`--timeout` is a wall-clock limit enforced by the official CLI; it accepts no
unlimited value. `run` defaults to 86400 seconds (Colab's maximum session
length) and other commands to 600. A local timeout does not stop the remote
process: check `colab status` and `colab log -s SESSION` and wait for it to
finish rather than repeating the command. `flush` only releases the lock and
unmounts storage. `rebuild` explicitly reinstalls saved virtual-environment
requirements. On a fresh changed image, use `rebuild-system` to reinstall the
recorded apt/system pip packages and start a new system chain before rebuilding
the virtual environment. `compact` compacts the current system layers; old
layers remain until an explicit `prune --yes`.

## Unattended operation with rclone

`colab drivemount` needs a TTY and a person to authorize Drive on every new VM.
The rclone backend replaces it, so a scheduler (for example a home server) can
run the whole cycle without interaction:

```sh
set -e
workspace() { colab-workspace --storage rclone --mount-point /mnt/pc --space research "$@"; }
colab new -s nightly
workspace mount-rclone -s nightly --conf ~/.config/rclone/colab.conf --remote colab:PersistentColabData
workspace restore -s nightly -p training
workspace run -s nightly -p training -f train.py -- --epochs 3
workspace finish -s nightly -p training
```

- `mount-rclone` validates the local config and remote name before any remote
  action, uploads the config with `colab upload` to a random `/tmp` path, moves
  it to `/root/.config/rclone/rclone.conf` (mode 600), installs rclone if
  needed and mounts with `--vfs-cache-mode writes`. Colab ships FUSE 2 only, so
  a private `fusermount3` shim is used. The daemon runs in its own session and
  survives `colab exec` calls and kernel restarts. The config contents never
  appear in generated exec code, which the official CLI keeps in `colab log`.
- With `--storage rclone`, the mounted remote is the shared data root. Workspaces
  live at `<mount-point>/spaces/<space>/workspaces/<project>`. The storage and
  space options can also follow workspace subcommands.
- `flush`/`finish` release the workspace lock once pending uploads have
  drained, wait for rclone's upload queue to empty, confirm this runtime's lock
  is gone from the remote, read `latest.json` back and compare the archive's
  size and MD5. System pointers, every referenced layer/manifest and the
  image baseline are verified too. It then unmounts and deletes every copy of the config. Only then is a receipt sent,
  and `finish` stops the VM only after that receipt. Any failure leaves the
  VM running, mounted and still able to retry.
- A non-zero `run` stops a `set -e` script before `finish`; the VM keeps
  running for inspection. Release it with `colab stop` after investigating.

Authentication of the official CLI itself must also be non-interactive on the
scheduler: reuse an existing `oauth2` login, or use `--auth adc` with
Application Default Credentials.

### A dedicated rclone remote

The uploaded config grants the VM whatever its token can access, and code run
in the workspace can read it until `finish`. Create a remote used only for
this purpose:

1. Create your own OAuth client ID (rclone's shared client is being retired).
2. `rclone config create colab drive client_id=... client_secret=... scope=drive.file`
   in a separate config file (`--config ~/.config/rclone/colab.conf`).
   `drive.file` only reaches files this rclone client created, so the token
   cannot read the rest of your Drive.
3. Use a folder rclone will create itself, such as
   `colab:PersistentColabData`. A `drive.file` token cannot see the existing
   `PersistentColab` folder made through Colab, and Drive allows two folders
   with the same name, so do not reuse that name.

Notebooks that use `drive.mount` see the rclone folder at
`/content/drive/MyDrive/PersistentColabData` when that is the actual remote folder.
`Workspace(PROJECT, space=SPACE)` uses this default data root. For a differently
named folder, pass the folder itself with `drive_root=Path('/content/drive/MyDrive/FOLDER')`,
not its `spaces` or `workspaces` subdirectory. The same Drive folder must be
visible to both clients; matching folder names alone do not prove object identity.
A `scope=drive` remote with `root_folder_id` limits which folder rclone shows,
but its token can still access the whole Drive.

Generate a self-contained notebook locally without allocating a runtime:

```sh
colab-workspace notebook --space research -p training -o training.ipynb
```

Existing output files are never overwritten. Skills under `skills/` can be
installed into `~/.codex/skills` with symlinks: `colab-persistence` describes this
workflow; `colab-operator` is the installed official CLI's bundled skill with a
local authentication note. Consult
live CLI help for current flags because the bundled official skill may describe
different authentication defaults from the installed release.

From the repository checkout, install the skills with:

```sh
python3 - <<'PY'
from pathlib import Path
destination = Path.home() / '.codex' / 'skills'
destination.mkdir(parents=True, exist_ok=True)
for name in ('colab-persistence', 'colab-operator'):
    (destination / name).symlink_to(Path.cwd() / 'skills' / name, target_is_directory=True)
PY
```

Keep the checkout available while using these symlinks. Existing skill
directories are preserved: the installer fails rather than replacing them.

## Python environment

Packages execute from `/content/persistent-colab/<space>/<project>/.venv`, using a virtual
environment with Colab's preinstalled packages available. Additional packages
and their executable scripts are archived on Drive. On the next compatible
runtime, a fresh virtual environment is created and the saved archive is
verified and restored locally. Running binaries directly on Drive is avoided.

`ws.install('package==version')` saves a new snapshot immediately. Ordinary
notebook imports use the restored package directories after `ws.activate()`.
For imports already present in the kernel, use `ws.run(code)` or restart the
kernel and bootstrap again. `%pip` and `!pip` target the system environment,
rather than the workspace virtual environment; save their changes with
`ws.save_environment()` or `colab-workspace save`. Use `ws.install` for workspace
packages and immediate saving. A failed `ws.install` is not committed.

Snapshots inherit the base image's Python packages, so Python major/minor,
architecture, libc and base package versions must match. A changed Colab image
produces an explicit error. Snapshots record the full base package list, and the
error names each changed package. When only base packages changed,
`prepare(allow_base_drift=True)` (CLI `--allow-base-drift`) restores anyway and
runs `pip check`; any conflict not already present in the base image removes
the restored environment again. Python, architecture or libc changes always
fail. Snapshots from before this list existed keep the strict check.

CPU and GPU runtimes do not share a base image. Keep separate spaces or projects
per runtime type. `--allow-base-drift` only relaxes the virtual environment's
base-package check; it cannot override a system image mismatch. `ws.rebuild()` on a fresh runtime reinstalls the
snapshot's frozen added-package versions and saves a new snapshot. Original
snapshots remain intact. CUDA extensions and device-specific compiled packages
may still require rebuilding when hardware, drivers or CUDA change. No GPU
compatibility is claimed from a CPU-only test.

Versioned index packages are supported. Editable/local/direct-URL packages are
not snapshot recipes: save project source in `src/` and keep setup cells in the
notebook. Secrets must use Colab Secrets; tokens are not intentionally archived.

## System environment

In real Colab VMs, `ws.prepare()` restores the saved system before preparing the
virtual environment. `ws.save_environment()` saves both. System persistence is
enabled automatically only in real Colab; local fixtures disable it by default.
The `system` constructor option explicitly controls this behavior.

On each fresh VM, mount storage and call `ws.prepare()` before installing
packages or changing system files, including the first time a project is used.
The initial baseline must describe the unmodified Colab image so your changes
are included in saved layers and package rebuild recipes.

The system snapshot records apt and system pip package differences plus new or
modified files under `/usr` (including `/usr/local`), `/opt`, `/etc`, `/var/lib`
and `/root/.local`. Capture uses ctime relative to PID 1 start on the root
filesystem, without following symlinks or crossing devices/bind mounts. A
per-image baseline path manifest records deletions, including obsolete dist-info
directories after system pip upgrades.
Volatile paths, known credential locations and other mounts are excluded.
This is not a secret detector: do not put arbitrary credentials into captured
directories. Services' configuration may persist, but their running processes
and the host GPU driver are not preserved. Colab containers lack
`CAP_SYS_MODULE`: kernel drivers come from the host and cannot be installed or
restored here. User-space CUDA/cuDNN/TensorRT files in captured roots are
included, but need validation on their actual GPU image and hardware.

System layers belong to a specific base image. An image mismatch fails explicitly;
on a fresh VM use `ws.rebuild_system()` (CLI `rebuild-system`) to reinstall the
recorded apt/system pip packages and begin a new chain. Arbitrary filesystem
changes from the old image are not replayed onto the new image: keep the setup
needed to reproduce them in source control. Then use `ws.rebuild()` (CLI
`rebuild`) for the saved virtual-environment requirements. Reinstallation can
fail if old package versions are unavailable or incompatible. Old chains remain
until explicitly pruned.

Each save writes only files changed since the previous save/restore, plus
deletions, into an immutable gzip layer and manifest. `pigz` is used when
available. SHA-256 readback precedes pointer publication; restore verifies the
complete chain before applying any layer and runs no apt/pip installer. Save
metadata records compressed bytes, duration and phase timings. Restore metrics
appear in `ws.local / 'system-restore.json'`; save metrics are returned and also
written to `ws.local / 'system-save.json'`. Both print phase seconds. The restore
report includes surrounding prepare, base-package signature and venv timings;
`ws.local / 'prepare.json'` also records these. Repeated prepare calls explicitly
report `already_applied` rather than reuse an earlier restore duration. An
interrupted filesystem apply requires a fresh VM. Keep sufficient local disk for the chain archives
and extracted files.

Restore derives its local tracking state from the verified baseline and layer
manifests, reading fingerprints only for paths written by the chain. Untouched
image paths use a local ctime checkpoint; the next save still scans all system
roots and detects later content/metadata changes, additions and deletions,
including writes that preserve mtime. Unknown runtime-created files remain new.
A directory-to-symlink change made before restore is rejected at save if it
would create inconsistent deletions. Prepare on a fresh VM before customizing it.

`ws.compact()` (CLI `compact`) combines the current system layers without deleting
old layers. `prune --keep N` is a dry run that includes unreferenced system
layers; `--yes` is required to delete. The current system chain is always kept.

CPU acceptance on 2026-10-15 used two genuinely separate, released VMs and the
shared rclone data root. VM A installed `cowsay`, `libxxhash-dev`, system
`pyfiglet`, upgraded base `wrapt` 2.5.0 to 2.5.1, added an executable under
`/usr/local/bin`, and installed venv `pyjokes`. VM B restored and used all of
them without package installation; the old wrapt dist-info directory was gone.
A durable output/checkpoint survived, `beta` remained absent, and a second
project's system restore was refused. Credential/runtime paths were excluded
from every layer. Notebook initialization used the same helper against the
rclone mount; interactive `drive.mount` in an actual notebook was not run.

### Restore performance

Measured on 2026-10-15 using separate fresh Standard CPU VMs, the same image
and rclone storage. Times start after mounting storage and exclude CLI transport,
allocation and mounting. Full prepare means `ws.prepare()`, excluding workspace
constructor directory setup. Small installation time is the earlier CPU
acceptance measurement; both small restores used the same 4-layer chain.

| Workload | Captured files / gzip | Plain installation | System restore | Full prepare |
| --- | --- | --- | --- | --- |
| Small apt/system pip example | 4.07 MB gzip, 4 layers | 17.05s | 30.25s | 34.78s |
| Boost + CUDA wheels + source-built lxml | 2.37 GB / 1.47 GB, 1 layer | 333.11s | 123.67s | 128.69s |

Small restore phase seconds before and after the optimization:

| Phase | Before | After |
| --- | ---: | ---: |
| Baseline load/check | 8.90 | 6.41 |
| Layer manifests | 9.21 | 5.23 |
| Archive copy/check/validation | 16.89 | 12.18 |
| Apply + ldconfig | 2.91 | 3.13 |
| Tracking state scan / derivation | 20.57 | 0.44 |
| Tracking state write | 2.52 | 0.71 |
| **System total, including other overhead** | **62.02** | **30.25** |

The unused pre-restore `pip list` (1.96s) is also gone. Post-restore signature
and venv work remain; full prepare fell from 66.48s to 34.78s, excluding that
former initialization call. Checksums, complete-chain validation, extraction
safety and the next save's full scan remain enabled.

The large workload installed `libboost-all-dev`, `libxml2-dev`, `libxslt1-dev`
(108 apt package changes), `nvidia-cudnn-cu12==9.10.2.21` with cuBLAS 12.9.2.10
and NVRTC 12.9.86, and built `lxml==6.0.2` from source. apt update/install took
38.24s, wheels 44.11s, source build 250.75s. System save took 127.96s (scan
23.42s, archive 66.91s, copy/readback 18.89s); asynchronous Drive flush is extra.
Large restore spent 57.66s copying/checking/validating, 53.17s applying,
4.63s loading the baseline, 2.26s on manifests, 2.73s in ldconfig and 1.13s
building/writing tracking state. Boost compiled/linked/ran, lxml parsed XML/XPath,
and cuDNN/NVRTC version calls and cuBLAS loading passed. A later save captured
only 447 bytes, not the restored gigabytes.

Allow roughly **30s fixed startup cost for a small 4-layer chain**, or **35s
including Python preparation**: a 17s install still wins. For the measured
2.37 GB payload (1.47 GB compressed), the break-even installation time was about
**130s**. There is no universal size threshold: apt plus these large wheels alone
took only 82.34s; the expensive source build made restoration worthwhile. Layer
count, Drive latency, compression and installation work matter. Saving is an
additional one-time cost. These are single-run measurements, not a throughput
guarantee; the first baseline creation on a new image also scans the filesystem.

GPU execution remains untested; loading CUDA user-space libraries on CPU does
not validate kernels, device compatibility or host drivers. Image migration,
compaction, corruption/deletion handling and finish gates also have local
filesystem/transport tests. Host kernel drivers cannot be installed or restored.

## Persistence boundaries

| Item | Behavior after releasing a runtime |
| --- | --- |
| Notebook, source and files saved on Drive | Remain on Drive |
| Added Python packages saved in a snapshot | Restored on a compatible new VM |
| Saved apt/system pip state and captured system files | Restored on the same base image; explicit package rebuild on a new image |
| Colab base libraries | Provided by the new VM; changes are detected |
| Model files in configured Hugging Face/Torch cache | Remain on Drive |
| Complete training checkpoints | Can resume if the training code supports it |
| RAM variables, GPU memory, executing processes | Lost |
| Unsaved files under `/content` | Lost |
| Running OS services and host GPU drivers | Not preserved |
| Volatile paths, known credential locations and other mounted filesystems | Excluded from system snapshots; save durable data explicitly |

`HF_HUB_CACHE` and `TORCH_HOME` point into the workspace on Drive. Neither
Drive's FUSE mount nor rclone's mount supports symlinks, so `huggingface_hub`
falls back to copying files instead of linking blobs, and first downloads are
slow because every byte is written through the mount. For large models,
download once and reuse the cache; or set `HF_HUB_CACHE` to local runtime disk
after `ws.prepare()` and copy the finished model into `ws.root / 'models'`.

Large archives need enough **runtime** disk to restore, regardless of Drive
capacity. Archives intentionally retain prior versions and are never pruned
automatically; use `colab-workspace prune` explicitly. Saving takes time proportional to environment size. Drive I/O and API
quotas remain applicable. Use one writer per project and save completed
checkpoints regularly. The workspace lock detects another runtime that already
holds the project; it is not a distributed mutex. Exclusive creation is atomic
within one runtime's mount, but Drive allows duplicate names and each mount
caches directory listings, so two runtimes starting at the same moment may both
succeed. Abrupt termination can lose work since the last completed write. Drive flush plus readback and a fresh-VM restore provide practical
verification, not a guarantee against cloud storage failure.

## Development

```sh
python3 build_notebook.py
python3 -m unittest discover -s tests
```

The notebook embeds the helper so it can bootstrap without a GitHub repository
or credentials. Regenerate it after helper changes.

Reference: [Google Colab FAQ](https://research.google.com/colaboratory/faq.html).

Third-party attribution: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
