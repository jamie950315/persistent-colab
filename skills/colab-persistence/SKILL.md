---
name: colab-persistence
description: Preserve and restore Python/AI workspaces on Google Drive across Colab VM releases and notebooks, using colab-workspace alongside the official colab CLI. Use for durable packages, system layers, spaces, files, checkpoints, or reusable Colab notebook setup.
---

# Colab persistence

Use `colab-workspace` for durable workspace operations and the official `colab`
CLI for VM lifecycle, authentication, transfers, and session history. The source
repository is `jamie950315/persistent-colab` on GitHub. Run `colab-workspace --help` and
`colab <command> --help` when command details are needed; the installed CLI's
help takes precedence over older bundled examples.

## Workflow

1. Inspect `colab sessions` and reuse the intended session. Create a named CPU
   session with `colab new -s NAME` when needed; choose GPU only for GPU work.
2. Mount storage on that VM. Unattended/agent use: `colab-workspace --storage
   rclone mount-rclone -s NAME --conf CONF --remote REMOTE:FOLDER`, then pass
   `--storage rclone` (and the same `--mount-point`, default `/mnt/pc`) to every
   later command. In a human terminal, `colab-workspace mount -s NAME` uses the
   official Drive mount, which requires a TTY and Google consent on each VM.
3. `colab-workspace restore -s NAME --space SPACE -p PROJECT` restores the saved
   system before the virtual environment. Both names default to `default`.
   Run it on a fresh VM before installing packages, changing system files or
   importing project dependencies, including the project's first use. It calls
   `ws.prepare()` to capture the unmodified image baseline so saved changes and
   package rebuild recipes include your later modifications.
4. Install with `colab-workspace install -s NAME --space SPACE -p PROJECT 'package==version'`.
   This saves immediately. `colab install`, `%pip`, and `!pip` target the system
   environment instead; save their changes explicitly with `save`.
5. `colab-workspace run -s NAME --space SPACE -p PROJECT -f job.py -- arg1` executes a fresh
   Python process with cwd at the durable workspace. Put source dependencies in
   `src/`; only the given script is transferred, not its adjacent local files.
6. Save full application/training checkpoints explicitly. When finished,
   `colab-workspace finish -s NAME --space SPACE -p PROJECT` saves both system
   and Python environments, releases the
   lock, flushes/unmounts storage and releases the named VM only after
   confirmed success. With rclone it also verifies the remote `latest.json` and
   archive, then deletes the config from the VM. `save` keeps storage mounted;
   `flush` unmounts it without stopping the VM.
7. `colab-workspace compact -s NAME --space SPACE -p PROJECT` compacts the system
   layer chain without deleting old layers. `prune -s NAME --space SPACE
   -p PROJECT --keep N` is a dry run for snapshots and unreferenced system layers;
   add `--yes` only when the user asked to delete them. The current chain is kept.

Pass the same `--space SPACE` to every project command; it can go before or after
the subcommand. `colab-workspace spaces -s NAME` lists spaces and projects using
the VM's already mounted storage. It does not allocate, mount or create a space.

Global `--auth`, `--config`, and `--client-oauth-config` options go before the
subcommand and are forwarded to the official CLI. Preserve existing credentials
and official session state. `doctor` checks version and sessions without renting
a VM. `status` reads workspace metadata without creating a project.

## Other notebooks, spaces and projects

Generate a self-contained notebook with:

```sh
colab-workspace notebook --space SPACE -p PROJECT -o workspace.ipynb
```

It refuses to overwrite an existing file. Copy its mount and restore cells into
another notebook when needed. Its self-contained helper uses:

```python
ws = Workspace('default', space='default')
ws.prepare()
ws.activate()
```

Run these mount and restore cells on a fresh VM before installing packages or
changing system files; the same baseline requirement applies to other notebooks.

The same space and project mean the same saved files/environments across
notebooks. Different names select separate directories, not separate access
permissions. Use one project per VM because system restoration changes the whole
VM, and one writer per project. Use a fresh VM to switch projects/spaces after
system restoration. Names use 1–64 letters, digits, underscores or hyphens,
starting with a letter or digit.

The private `My Drive/PersistentColab` notebook, helper and README were updated
in place on 2026-10-15, preserving file IDs and sharing. Re-sync them after
helper/notebook changes; the controller handles interactive Drive mount consent.
Preserve private-output settings and existing data; do not migrate or rename data
without explicit authorization.

## Persistence and recovery boundaries

- Durable files:
  `/content/drive/MyDrive/PersistentColabData/spaces/SPACE/workspaces/PROJECT`
  (Drive mount) or `<mount-point>/spaces/SPACE/workspaces/PROJECT` (rclone).
- `drive_root` is the shared data root, not a `workspaces` subdirectory. Mount
  the same shared remote folder (for example `colab:PersistentColabData`) for
  every rclone space. Matching folder names alone do not prove Drive identity.
- Executable packages: `/content/persistent-colab/SPACE/PROJECT/.venv` on the VM.
- Virtual-environment snapshots inherit Colab base packages and validate the
  Python/base-package signature and archive checksum. Use explicit `rebuild`
  for their requirements after rebuilding a changed system image on a fresh VM;
  keep previous snapshots. Do not silently rebuild or prune versions.
- System persistence automatically enables only on real Colab VMs; local
  fixtures disable it by default (`Workspace(..., system=None)`). Save captures
  apt/system pip package state and changes under `/usr` (including `/usr/local`),
  `/opt`, `/etc`, `/var/lib` and `/root/.local`, excluding volatile paths, known credential
  locations and other mounts. This does not detect arbitrary secrets stored in
  captured directories. Keep required setup in source control.
- System layers require the same base image. On a fresh changed image,
  `rebuild-system` explicitly reinstalls recorded apt/system pip packages and
  starts a new chain, retaining the old one. Arbitrary filesystem changes are
  not replayed onto a different image. Then use `rebuild` for virtual-environment
  requirements. Do not silently rebuild, compact or prune.
- RAM, GPU memory, live jobs, unsaved `/content` files, running services and host
  GPU drivers are not preserved. Drive capacity does not enlarge runtime disk.
- Keep tokens in Colab Secrets or temporary runtime storage, not checkpoints or
  snapshots. Editable/local/direct-URL installations are rejected snapshot inputs.
- `restore`/`install`/`run`/`save`/`compact`/rebuild commands claim `environment/LOCK`. "In use" means
  another runtime is active: do not override it. A stale lock needs
  `--force-unlock` only after confirming that runtime is gone (`colab sessions`).
- A base-package-only image change lists the changed packages. Use
  `--allow-base-drift` to restore with a `pip check` rollback, or `rebuild`.
  This only relaxes virtual-environment checks; it does not bypass the system
  image check. Keep separate spaces/projects for CPU and GPU runtime images.
- Never print, cat, upload elsewhere or copy rclone configs into the workspace,
  outputs or snapshots. Use a dedicated `drive.file` remote.
- `run` defaults to a 24-hour timeout; others to 600 seconds. Official exec
  cannot run unlimited.
- A timeout or error is not completion. Inspect `colab status` and `colab log`;
  do not repeat installs/saves or stop a VM before checking the running operation.
- Shared-root spaces/system persistence passed two separate CPU VMs: apt binary
  and library, system pip base upgrade (obsolete dist-info removed), custom
  `/usr/local/bin` executable, venv package, output/checkpoint, small incremental
  layer, credential exclusion and save/flush/stop. The notebook helper used the
  same rclone root; actual notebook Drive consent was not exercised. Small
  environments can still restore more slowly than installation. Fresh CPU
  measurements: 4.07 MB/4 layers restore 30.25s (prepare 34.78s) versus 17.05s
  install; 1.47 GB gzip restore 123.67s (prepare 128.69s) versus 333.11s install,
  dominated by a 250.75s source build. Size alone is not a break-even rule.
  Inspect `ws.local / 'system-restore.json'`, `system-save.json` and `prepare.json`
  for phases. The next save still scans all roots; only restore's tracking-state
  scan was removed. GPU execution needs validation on actual hardware.
- Colab lacks `CAP_SYS_MODULE`; host kernel drivers cannot be installed here.
  User-space CUDA/cuDNN/TensorRT files in captured roots are saved, but CPU
  acceptance does not prove GPU compatibility.
- Browser links to Drive notebooks may allocate another VM instead of reusing a
  CLI VM. Inspect server assignments and release only task-owned runtimes.
