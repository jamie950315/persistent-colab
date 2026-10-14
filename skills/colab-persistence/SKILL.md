---
name: colab-persistence
description: Preserve and restore Python/AI workspaces on Google Drive across Colab VM releases and notebooks, using colab-workspace alongside the official colab CLI. Use for durable packages, files, checkpoints, or reusable Colab notebook setup.
---

# Colab persistence

Use `colab-workspace` for durable workspace operations and the official `colab`
CLI for VM lifecycle, authentication, transfers, and session history. The local
source is `/Users/jamie/persistent-colab`. Run `colab-workspace --help` and
`colab <command> --help` when command details are needed; the installed CLI's
help takes precedence over older bundled examples.

## Workflow

1. Inspect `colab sessions` and reuse the intended session. Create a named CPU
   session with `colab new -s NAME` when needed; choose GPU only for GPU work.
2. Mount Drive on that VM. In a human terminal use
   `colab-workspace mount -s NAME`; agents should use the notebook mount cell
   and complete the authorized Google consent flow. CLI mount requires a TTY.
3. `colab-workspace restore -s NAME -p PROJECT` restores the saved environment.
4. Install with `colab-workspace install -s NAME -p PROJECT 'package==version'`.
   This saves immediately. `colab install`, `%pip`, and `!pip` target the base
   runtime and do not save this environment.
5. `colab-workspace run -s NAME -p PROJECT -f job.py -- arg1` executes a fresh
   Python process with cwd at the durable workspace. Put source dependencies in
   `src/`; only the given script is transferred, not its adjacent local files.
6. Save full application/training checkpoints explicitly. When finished,
   `colab-workspace finish -s NAME -p PROJECT` saves packages, flushes/unmounts
   Drive and releases the named VM only after confirmed success. `save` keeps
   Drive mounted; `flush` unmounts it without stopping the VM.

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

- Durable files: `/content/drive/MyDrive/PersistentColab/workspaces/PROJECT`.
- Executable packages: `/content/persistent-colab/PROJECT/.venv` on the VM.
- Snapshots inherit Colab base packages and validate Python/base-image signature
  and archive checksum. A changed image requires explicit `rebuild` on a fresh
  VM; keep previous snapshots. Do not silently rebuild or prune versions.
- RAM, GPU memory, live jobs, unsaved `/content` files, apt packages and drivers
  are not preserved. Drive capacity does not enlarge runtime disk.
- Keep tokens in Colab Secrets or temporary runtime storage, not checkpoints or
  snapshots. Editable/local/direct-URL installations are rejected snapshot inputs.
- A timeout or error is not completion. Inspect `colab status` and `colab log`;
  do not repeat installs/saves or stop a VM before checking the running operation.
- CPU restore across released VMs and a separate notebook, plus separation of
  project files/checkpoints/saved packages, were tested. GPU/CUDA compatibility
  needs verification for the actual hardware-dependent workload.
- Browser links to Drive notebooks may allocate another VM instead of reusing a
  CLI VM. Inspect server assignments and release only task-owned runtimes.
