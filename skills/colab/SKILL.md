---
name: colab
description: Run work on Google Colab through the colab CLI, manage CPU/GPU/TPU sessions, and preserve or restore Drive-backed environments, spaces, files and checkpoints across runtimes.
---

# Colab

Use the official `colab` CLI for VM lifecycle and transport, and
`colab-workspace` for durable environments. Live CLI help takes precedence over
bundled command examples. These tools do not keep a VM or live process alive.

## Choose the workflow

- **Continuing work or reusable installations:** use the persistent workflow
  below. Read [persistence details](references/persistence.md) for snapshot,
  notebook, image compatibility, rebuild and recovery operations.
- **Explicitly disposable jobs:** use the official CLI without creating a
  persistent environment. Retrieve required outputs before releasing the VM.
  `colab run` automatically tears down unless `--keep` is supplied, so use it
  only when that lifecycle fits the task.
- Read [operator details](references/operator.md) for provisioning, transfers,
  execution, authentication and session inspection. The finish rules here
  govern persistent jobs, including when using official CLI commands.

## Persistent workflow

1. Inspect `colab sessions`; reuse only the intended session. Otherwise create
   a named CPU session with `colab new -s NAME`. Request GPU/TPU only when the
   workload needs it and within the user's compute authorization.
2. Mount the shared data root. For unattended use on this Mac:
   ```sh
   colab-workspace --storage rclone mount-rclone -s NAME \
     --conf ~/.config/rclone/colab.conf --remote colab:PersistentColabData
   ```
   Pass `--storage rclone` to every subsequent workspace command, with the same
   mount point if customized. Other devices need their own authorized config.
3. On a fresh VM, run restore **before installing packages or changing system
   files, even on first use**, to record the pristine baseline:
   ```sh
   colab-workspace --storage rclone restore -s NAME --space SPACE -p PROJECT
   ```
4. Use the same space/project throughout. Install venv packages with
   `colab-workspace install`. Apt and system pip changes are also captured,
   but require an explicit `save` or `finish`. Use `run -f job.py -- args`
   for execution; only that script is transferred, not adjacent dependencies.
   Store source, outputs and application checkpoints in the durable workspace.
5. Save periodically for long work. Complete persistent jobs with:
   ```sh
   colab-workspace --storage rclone finish -s NAME --space SPACE -p PROJECT
   ```
   This saves, verifies remote data, flushes/unmounts, removes VM rclone
   credentials and only then releases the VM. If it fails, inspect the failure
   and leave the VM available for recovery; do not substitute `colab stop`.

## Operational boundaries

- Reuse existing authentication. Do not replace credentials or edit official
  session-state files. This Mac uses OAuth2; bundled upstream ADC defaults do
  not override installed CLI behavior.
- Never print config contents or put secrets in exec code, snapshots or durable
  outputs: official `colab log` records exec code. Upload configs only through
  the workspace mount command's staging path.
- Agent workflows use rclone, not interactive `drivemount`/`auth`. A human
  notebook can use official Drive consent; it reads the same shared data root.
- One project per VM: restoring system files affects the whole runtime. Use a
  fresh VM to switch projects/spaces. Spaces separate folders, not permissions;
  use one writer per project.
- Never override an active lock. Confirm its runtime is gone before using
  `--force-unlock`. Do not silently rebuild, compact or prune snapshots;
  deletion requires the user's authorization.
- System layers require the same base image. CPU/GPU images differ.
  `--allow-base-drift` only relaxes venv checks, not system image compatibility.
  Read the persistence reference before rebuilding on a changed image.
- RAM, GPU memory, live jobs, running services and host kernel drivers do not
  persist. User-space CUDA libraries can be captured; CPU tests do not prove
  GPU compatibility. Arbitrary secrets in captured system paths remain unsafe.
- A timeout does not prove execution stopped. Inspect status/log before
  repeating work. Official exec can return zero after a remote Python error;
  workspace commands use structured receipts, especially before VM release.
