---
name: colab-persistence
description: Preserve and restore Python/AI workspaces on Google Drive across Colab VM releases and notebooks, using colab-workspace alongside the official colab CLI. Use for durable packages, files, checkpoints, or reusable Colab notebook setup.
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
3. `colab-workspace restore -s NAME -p PROJECT` restores the saved environment.
4. Install with `colab-workspace install -s NAME -p PROJECT 'package==version'`.
   This saves immediately. `colab install`, `%pip`, and `!pip` target the base
   runtime and do not save this environment.
5. `colab-workspace run -s NAME -p PROJECT -f job.py -- arg1` executes a fresh
   Python process with cwd at the durable workspace. Put source dependencies in
   `src/`; only the given script is transferred, not its adjacent local files.
6. Save full application/training checkpoints explicitly. When finished,
   `colab-workspace finish -s NAME -p PROJECT` saves packages, releases the
   lock, flushes/unmounts storage and releases the named VM only after
   confirmed success. With rclone it also verifies the remote `latest.json` and
   archive, then deletes the config from the VM. `save` keeps storage mounted;
   `flush` unmounts it without stopping the VM.
7. `colab-workspace prune -s NAME -p PROJECT --keep N` is a dry run; add
   `--yes` only when the user asked to delete old snapshots.

Global `--auth`, `--config`, and `--client-oauth-config` options go before the
subcommand and are forwarded to the official CLI. Preserve existing credentials
and official session state. `doctor` checks version and sessions without renting
a VM. `status` reads workspace metadata without creating a project.

## Other notebooks and workspace names

Generate a self-contained notebook with:

```sh
colab-workspace notebook -p PROJECT -o workspace.ipynb
```

It refuses to overwrite an existing file. For the existing Drive deployment,
another notebook can initialize the same helper with:

```python
from google.colab import drive
from pathlib import Path
drive.mount('/content/drive')
exec(Path('/content/drive/MyDrive/PersistentColab/persistent_colab.py').read_text())
ws = Workspace('default')
ws.prepare()
ws.activate()
```

Same project name means the same saved files/environment across notebooks.
Different names select separate directories, not separate access permissions.
Use one writer per project and separate kernels for different projects: already
imported Python modules cannot be switched safely by changing a path.

## Persistence and recovery boundaries

- Durable files: `/content/drive/MyDrive/PersistentColab/workspaces/PROJECT`
  (Drive mount) or `<mount-point>/workspaces/PROJECT` (rclone).
- Executable packages: `/content/persistent-colab/PROJECT/.venv` on the VM.
- Snapshots inherit Colab base packages and validate Python/base-image signature
  and archive checksum. A changed image requires explicit `rebuild` on a fresh
  VM; keep previous snapshots. Do not silently rebuild or prune versions.
- RAM, GPU memory, live jobs, unsaved `/content` files, apt packages and drivers
  are not preserved. Drive capacity does not enlarge runtime disk.
- Keep tokens in Colab Secrets or temporary runtime storage, not checkpoints or
  snapshots. Editable/local/direct-URL installations are rejected snapshot inputs.
- `restore`/`install`/`run`/`save` claim `environment/LOCK`. "In use" means
  another runtime is active: do not override it. A stale lock needs
  `--force-unlock` only after confirming that runtime is gone (`colab sessions`).
- A base-package-only image change lists the changed packages. Use
  `--allow-base-drift` to restore with a `pip check` rollback, or `rebuild`.
  CPU and GPU runtimes have different base images.
- Never print, cat, upload elsewhere or copy rclone configs into the workspace,
  outputs or snapshots. Use a dedicated `drive.file` remote.
- `run` defaults to a 24-hour timeout; others to 600 seconds. Official exec
  cannot run unlimited.
- A timeout or error is not completion. Inspect `colab status` and `colab log`;
  do not repeat installs/saves or stop a VM before checking the running operation.
- CPU restore across released VMs and a separate notebook, plus separation of
  project files/checkpoints/saved packages, were tested. GPU/CUDA compatibility
  needs verification for the actual hardware-dependent workload.
- Browser links to Drive notebooks may allocate another VM instead of reusing a
  CLI VM. Inspect server assignments and release only task-owned runtimes.
