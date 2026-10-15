"""Build a self-contained Colab notebook from the reviewed workspace helper."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def cell(kind, text):
    item = {"cell_type": kind, "metadata": {}, "source": text.splitlines(keepends=True)}
    if kind == "code":
        item.update(execution_count=None, outputs=[])
    return item


cells = [
    cell("markdown", """# Persistent Colab workspace

Keep files, a saved Python environment and system installation layers in Google Drive, then restore them on a compatible new runtime.

**Start:** choose CPU for ordinary development, or select a GPU when needed. On a fresh VM, run cells 1 and 2 before installing packages, changing system files or importing project dependencies, including the first time you use a project. This records the unmodified image baseline so saved changes and package rebuild recipes are accurate. Grant Drive access if Google asks. Use the same SPACE and PROJECT on future visits. Data lives under `My Drive/PersistentColabData/spaces/SPACE/workspaces/PROJECT`.

**Finish:** save model/training checkpoints, run **Save environment**, then **Flush Drive**. Finally use **Runtime → Disconnect and delete runtime** to release compute.

Drive preserves files and saved environment archives. A released VM loses RAM, GPU memory, running programs and unsaved changes. System layers require the same base image; changing images requires an explicit rebuild on a fresh VM. Host GPU drivers are not saved. Google Drive capacity does not enlarge the runtime disk.
"""),
    cell("code", """# @title 1. Mount Google Drive
from google.colab import drive
drive.mount('/content/drive')
"""),
    cell("code", "# @title 2. Restore workspace, system and Python environments\n" +
         "SPACE = 'default'  # @param {type:\"string\"}\n" +
         "PROJECT = 'default'  # @param {type:\"string\"}\n" +
         "exec(" + repr((ROOT / "persistent_colab.py").read_text()) + ")\n" +
         "ws = Workspace(PROJECT, space=SPACE)\nws.prepare()\nws.activate()\n" +
         "print('Previous saved state:', ws.read_checkpoint())\n"),
    cell("markdown", """## Work here

Your current directory is the durable Drive workspace. Use `ws.root / 'src'`, `'data'`, `'models'`, `'outputs'`, and `'checkpoints'` for files that must survive.

Install workspace Python packages with `ws.install('package==version')`; it saves the environment immediately. The environment inherits Colab's preinstalled AI libraries. `%pip` and `!pip` affect the system environment instead; save those changes with `ws.save_environment()`. Use `ws.run(code)` or `subprocess.run([str(ws.python), ...])` for an independent Python process and for packages already imported by the notebook kernel. Installing a replacement for an already loaded package requires restarting the kernel and running cells 1 and 2 again.

Hugging Face model downloads and Torch downloads use the Drive model directory. Authentication tokens stay on the temporary runtime. Keep secrets in Colab Secrets; never in the notebook, workspace or checkpoints.

For many small files, unpack a dataset on the runtime's local disk for speed, retaining its original archive on Drive. Only files actually saved back to Drive survive. Use one project per VM: system restoration changes the whole VM. Spaces and projects separate directories, not account permissions. Concurrent writers to the same project are unsupported.
"""),
    cell("code", """# @title Optional: install and save packages
# Uncomment and change the package list when needed.
# ws.install('humanize==4.15.0')
"""),
    cell("code", """# @title Your Python / AI code
from pathlib import Path
print('Working directory:', Path.cwd())
print('Persistent output directory:', ws.root / 'outputs')
print('Environment Python:', ws.python)
# Write your code in this cell or add more cells here.
"""),
    cell("markdown", """## Save progress explicitly

Use `ws.write_checkpoint({'step': 100, ...})` for JSON state; load it with `ws.read_checkpoint()`. This is application-defined state, not an automatic copy of Python variables.

For Hugging Face Trainer, set `output_dir=str(ws.root / 'checkpoints' / 'training')`, save checkpoints regularly, and use `trainer.train(resume_from_checkpoint=...)` to resume a complete checkpoint. Saving model weights alone does not preserve optimizer, scheduler and training progress. Keep the model, dataset and training configuration compatible when resuming.

`ws.save_environment()` also saves apt/system pip installation state and filesystem changes under `/usr/local`, `/opt`, `/etc`, `/var/lib` and `/root/.local`. It excludes volatile paths, known credential locations and other mounts. Keep secrets in Colab Secrets; arbitrary secrets placed in captured directories can still be archived. Configuration files may persist, but running OS services and host GPU drivers do not.
"""),
    cell("code", """# @title Save environment
saved_environment = ws.save_environment()
print('Saved at:', saved_environment['created_utc'])
"""),
    cell("markdown", """## Before releasing compute

Check **FLUSH_DRIVE** and run the next cell **only when finished**. It releases the workspace lock, then flushes and unmounts Drive. Then disconnect and delete the runtime using the Runtime menu. To continue working after unmounting, mount Drive again.

If Colab reports a system/base-image mismatch, start a fresh runtime, mount Drive and instantiate `ws` from cell 2. Run `ws.rebuild_system()` to explicitly reinstall recorded apt/system pip packages and start a new system layer chain; arbitrary files from the old image are not replayed onto a different image. Then use `ws.rebuild()` for saved virtual-environment package requirements and `ws.activate()`. Keep required manual setup in source control. Reinstallation can fail when packages no longer support the new image; old snapshots remain on Drive.

`ws.compact()` replaces the current system layer chain with a compact layer; old layers remain until an explicit prune. CPU and GPU runtimes use different base images. `ws.prepare(allow_base_drift=True)` only relaxes virtual-environment base-package checks with `pip check`; it does not bypass the system image check.

Cell 2 claims the workspace for this runtime. If another runtime ended without flushing, the error reports a stale lock after 10 minutes; once you are sure that runtime is gone, use `ws.prepare(force_unlock=True)`.
"""),
    cell("code", """# @title Flush Drive (run manually when finished)
FLUSH_DRIVE = False  # @param {type:"boolean"}
if FLUSH_DRIVE:
    ws.flush()
else:
    print('Check FLUSH_DRIVE and run this cell when you are finished.')
"""),
]
for index, item in enumerate(cells):
    item["id"] = f"persistent-colab-{index}"
    item["metadata"]["id"] = item["id"]
    if index in (1, 2, 7, 9):
        item["metadata"]["cellView"] = "form"

notebook = {"nbformat": 4, "nbformat_minor": 5,
            "metadata": {"colab": {"name": "PersistentColab.ipynb", "provenance": [],
                                    "private_outputs": True},
                         "kernelspec": {"name": "python3", "display_name": "Python 3"},
                         "language_info": {"name": "python"}},
            "cells": cells}
(ROOT / "PersistentColab.ipynb").write_text(json.dumps(notebook, indent=2) + "\n")
print("Built PersistentColab.ipynb")
