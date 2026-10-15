# Persistent Colab

## Purpose
Provide a general Python/AI workspace whose files, saved Python environment and
system installation layers survive the release of a managed Colab runtime.
This does not keep a VM or live process alive.

## Layout
- `persistent_colab.py`: Drive-backed spaces/projects, system layers and local virtual environment.
- `build_notebook.py`: generates the self-contained `PersistentColab.ipynb`.
- `README.md`: operation and persistence boundaries.
- `tests/`: integrity and compatibility checks for environment snapshots.
- `colab_workspace_cli.py` and `pyproject.toml`: installed `colab-workspace`
  companion using the official `colab` executable as its transport.
- `skills/colab/`: unified Colab skill with persistence and attributed official
  CLI references; the only installed Colab skill entrypoint.

## Operation
- The private Drive deployment in `My Drive/PersistentColab` was updated in place
  on 2026-10-15 with the current notebook, helper and README (same file IDs and
  sharing). Re-sync these files after helper/notebook changes; preserve sharing,
  private-output settings and existing data. The controller handles the one
  interactive Drive mount consent required on a VM. The first `colab drivemount`
  after consent has twice failed with "mount failed" (DriveFS could not read the
  VM's auth user-id); rerunning it on the same VM reuses the grant and succeeds.
  Overwrite files in place (truncate and write) so file IDs stay unchanged.
- Shared data root: `/content/drive/MyDrive/PersistentColabData`, or the mounted
  remote folder with `--storage rclone`. `drive_root` now means this data root.
- Persistent project root: `<data-root>/spaces/<space>/workspaces/<project>`.
  CLI `--space` defaults to `default` and accepts either subcommand position;
  `spaces -s SESSION` lists existing spaces/projects on mounted storage only.
- Runtime environment: `/content/persistent-colab/<space>/<project>/.venv`.
- `Workspace(..., space='default', system=None)` enables system persistence
  automatically only on real Colab; local fixtures disable it by default.
  `prepare` restores system before the virtual environment; `save_environment`
  saves both. System state includes apt/system pip and changes under
  `/usr` (including `/usr/local`), `/opt`, `/etc`, `/var/lib`, `/root/.local`,
  excluding volatile paths, known credentials and other mounts. Arbitrary secrets in captured
  directories are still unsafe; host GPU drivers and running services do not persist.
- On a fresh VM, mount storage and run `prepare` (CLI `restore`) before any
  project/system package installation or system-file customization, including
  first use. The pristine baseline keeps captured deltas and rebuild recipes
  accurate.
- System layers require the same base image. On a fresh changed image,
  `rebuild-system` explicitly reinstalls recorded apt/system pip packages and
  starts a new chain; `rebuild` retains its virtual-environment meaning.
  `--allow-base-drift` never bypasses the system image check.
- `compact` compacts the system chain without deleting old layers. Explicit
  `prune --keep N` also lists unreferenced system layers and keeps the current
  chain; deletion requires `--yes`. Never prune automatically.
- Use one project per VM because system restore changes the whole VM; start a
  fresh VM to switch projects/spaces after system restoration. Use one snapshot
  writer per project. Spaces provide directory separation, not access isolation.
- Use a CPU runtime for setup/verification; request a GPU only for GPU work.
- Keep credentials and private runtime/Drive metadata out of Git.
- Snapshot versions are immutable; never prune user snapshots automatically.
- Run `python3 build_notebook.py` after changing the helper, then
  `python3 -m unittest discover -s tests`.
- Verify restoration in a genuinely new Colab VM after persistence changes.
- CPU acceptance (2026-10-15) passed across two separately released VMs:
  apt binary/library, system pip base upgrade with obsolete dist-info removal,
  custom executable, venv package, durable output/checkpoint, small incremental
  layer, credential exclusions, second-project refusal and save/flush/stop.
  The notebook helper used the same rclone root; actual notebook `drive.mount`
  consent and GPU execution remain untested. Restore performance (2026-10-15,
  fresh CPU VMs): 4.07 MB/4 layers fell from 62.02s to 30.25s (prepare 34.78s),
  versus the earlier 17.05s install. A 2.37 GB file payload/1.47 GB gzip restored
  in 123.67s (prepare 128.69s) versus 333.11s install, including a 250.75s lxml
  source build. Save took 127.96s plus Drive flush. A later save was 447 bytes.
  Boost/lxml and CUDA user-space load/version checks passed on CPU. Budget about
  35s startup for the small chain or 130s for that large payload; size alone is
  not a break-even rule. See README for phases, timing scope and limitations.
- Restore tracking state uses verified baseline/layer paths, fingerprints for
  layer-written paths, and a local filesystem ctime checkpoint for untouched
  image paths. The next save still scans all roots; equal clock ticks count as
  changed. Reject pre-restore directory-to-symlink drift if it would create
  inconsistent deletions. Restore/save phase metrics live in `system-restore.json`
  and `system-save.json` under `ws.local`; `prepare.json` covers surrounding work.
- Colab lacks `CAP_SYS_MODULE`; kernel GPU drivers come from the host. Persist
  user-space libraries only; validate them on their actual GPU image/hardware.
- The companion requires an explicit session name. It does not auto-allocate,
  auto-authorize Drive or change official credentials/session-state files.
- Remote commands use temporary notebooks and structured success receipts:
  official `colab exec` 0.7.4 can return zero after a remote Python error.
  `finish` must not stop a runtime until save and Drive flush confirm success.
- Shared rclone data root: `colab:PersistentColabData` (server-side renamed
  from the empty rclone-created `PersistentColabRclone` folder), dedicated
  `drive.file` config `~/.config/rclone/colab.conf`. Preserve this config.
  `finish` verifies the venv snapshot, system chain/manifests and image baseline
  on the remote before unmounting/removing VM credentials and stopping the VM.
  Deleting a file whose rewrite is still queued in rclone's VFS leaves the old
  remote object; wait for uploads before deleting. Colab ships FUSE 2 only, so a `fusermount3` shim is needed;
  the mount must start in a new session or a kernel restart unmounts it.
  `/tmp` upload paths are absolute (the Jupyter root is `/`). Writing into an
  unmounted mount point silently writes to VM disk; always check mounts first.
  Config contents must never enter exec code: `colab log` stores it.
- Official `exec --timeout` is a wall-clock deadline; 0 and `inf` do not work.
- CPU and GPU images differ. Existing base-image `pip check` conflicts must
  remain separate from new workspace conflicts.
- Public repository: `https://github.com/jamie950315/persistent-colab`, branch `main`.
- Install from a local checkout with `uv tool install --editable .`.
  Installed `colab` skill entries in `~/.codex/skills` and `~/.claude/skills`
  link to tracked `skills/colab/`; retire old operator/persistence symlinks.
- The installed companion was tested on a real CPU VM through official CLI
  0.7.4: Drive mount, existing snapshot restore, script execution/arguments,
  exit-code propagation, package installation, status, and save/flush/stop.
  The test VM was released. CLI protocol/release-gate tests use local transport
  fixtures; helper persistence checks remain independent.
- The official operator skill is derived from `googlecolab/google-colab-cli`
  0.7.4. Preserve its attribution and Apache-2.0 license in `THIRD_PARTY_NOTICES.md`
  and `third_party/google-colab-cli/LICENSE` when distributing it.
