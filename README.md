# Persistent Colab

A general Python/AI workspace stored in Google Drive. Releasing a managed Colab
runtime releases its VM; reopening the notebook restores durable files and the
last saved Python environment into a new runtime.

## Use

1. Open `PersistentColab.ipynb` in Colab. Use CPU unless the task needs a GPU.
2. Run **Mount Google Drive**, then **Restore workspace and Python environment**.
3. Keep the same `PROJECT` name to continue the same workspace.
4. Write durable files under `ws.root`. Install packages using `ws.install(...)`.
5. Save application/model checkpoints during long jobs.
6. Run **Save environment**, check **FLUSH_DRIVE**, then run **Flush Drive** when finished.
7. Use **Runtime → Disconnect and delete runtime**. Reopen and run steps 1–2
   next time; hardware can be selected separately from the saved workspace.

```
My Drive/PersistentColab/
  PersistentColab.ipynb
  persistent_colab.py
  workspaces/default/
    src/
    data/
    models/
    outputs/
    checkpoints/
    environment/
      latest.json
      snapshots/<timestamp>.tar.gz
      snapshots/<timestamp>.json
```

## Use from another notebook

Add this initialization cell before importing project packages:

```python
from google.colab import drive
from pathlib import Path

drive.mount('/content/drive')
exec(Path('/content/drive/MyDrive/PersistentColab/persistent_colab.py').read_text())
ws = Workspace('default')
ws.prepare()
ws.activate()
```

The same project name shares the durable files and saved environment across
notebooks. A different name creates separate workspace and environment
directories. Initialize each notebook explicitly; this is not an account-wide
Colab setting. Keep one writer per project, and use separate kernels for different
projects to avoid already imported modules carrying over. All projects remain
accessible to the same Drive account; separate directories are not a security
boundary.

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
colab-workspace mount -s development        # Interactive terminal authorization.
colab-workspace restore -s development -p default
colab-workspace install -s development -p default 'humanize==4.15.0'
colab-workspace run -s development -p default -f job.py -- input.csv
colab-workspace status -s development -p default
colab-workspace save -s development -p default
colab-workspace finish -s development -p default
```

`run` uses an independent workspace Python process, passes script arguments and
its exit code, and sets the durable workspace as cwd. Imports can use `src/` and
the workspace root. Only the specified script is transferred; upload source or
data dependencies separately with official `colab upload`.

`finish` saves the Python environment, flushes/unmounts Drive and releases only
the named VM after a structured success receipt. Save application checkpoints
beforehand. A remote Python error, timeout or missing receipt leaves the VM
running for inspection, even when the official `colab exec` exits zero.
`--timeout` is a wall-clock limit enforced by the official CLI; it accepts no
unlimited value. `run` defaults to 86400 seconds (Colab's maximum session
length) and other commands to 600. A local timeout does not stop the remote
process: check `colab status` and `colab log -s SESSION` and wait for it to
finish rather than repeating the command. `flush` only unmounts Drive. `rebuild` explicitly
reinstalls saved requirements on a fresh changed image.

Generate a self-contained notebook locally without allocating a runtime:

```sh
colab-workspace notebook -p training -o training.ipynb
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

Packages execute from `/content/persistent-colab/<project>/.venv`, using a virtual
environment with Colab's preinstalled packages available. Additional packages
and their executable scripts are archived on Drive. On the next compatible
runtime, a fresh virtual environment is created and the saved archive is
verified and restored locally. Running binaries directly on Drive is avoided.

`ws.install('package==version')` saves a new snapshot immediately. Ordinary
notebook imports use the restored package directories after `ws.activate()`.
For imports already present in the kernel, use `ws.run(code)` or restart the
kernel and bootstrap again. `%pip` and `!pip` do not target this environment;
use `ws.install` for persistence. A failed install is not committed.

Snapshots inherit the base image's Python packages, so Python major/minor,
architecture, libc and base package versions must match. A changed Colab image
produces an explicit error. `ws.rebuild()` on a fresh runtime reinstalls the
snapshot's frozen added-package versions and saves a new snapshot. Original
snapshots remain intact. CUDA extensions and device-specific compiled packages
may still require rebuilding when hardware, drivers or CUDA change. No GPU
compatibility is claimed from a CPU-only test.

Versioned index packages are supported. Editable/local/direct-URL packages are
not snapshot recipes: save project source in `src/` and keep setup cells in the
notebook. Secrets must use Colab Secrets; tokens are not intentionally archived.

## Persistence boundaries

| Item | Behavior after releasing a runtime |
| --- | --- |
| Notebook, source and files saved on Drive | Remain on Drive |
| Added Python packages saved in a snapshot | Restored on a compatible new VM |
| Colab base libraries | Provided by the new VM; changes are detected |
| Model files in configured Hugging Face/Torch cache | Remain on Drive |
| Complete training checkpoints | Can resume if the training code supports it |
| RAM variables, GPU memory, executing processes | Lost |
| Unsaved files under `/content` | Lost |
| apt packages, OS services, drivers | Rerun explicit setup cells |

Large archives need enough **runtime** disk to restore, regardless of Drive
capacity. Archives intentionally retain prior versions and are not automatically
pruned. Saving takes time proportional to environment size. Drive I/O and API
quotas remain applicable. Use one writer per project and save completed
checkpoints regularly: abrupt termination can lose work since the last completed
write. Drive flush plus readback and a fresh-VM restore provide practical
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
