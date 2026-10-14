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

Keep files and a saved Python environment in Google Drive, then restore them on a new CPU or GPU runtime.

**Start:** choose CPU for ordinary development, or select a GPU when needed. Run cells 1 and 2 before importing your own packages. Grant Drive access if Google asks. Use the same project name on future visits.

**Finish:** save model/training checkpoints, run **Save environment**, then **Flush Drive**. Finally use **Runtime → Disconnect and delete runtime** to release compute.

Drive preserves files and saved environment archives. A released VM loses RAM, GPU memory, running programs and unsaved changes. A Python/base-image change requires an explicit environment rebuild. Google Drive capacity does not enlarge the runtime disk.
"""),
    cell("code", """# @title 1. Mount Google Drive
from google.colab import drive
drive.mount('/content/drive')
"""),
    cell("code", "# @title 2. Restore workspace and Python environment\n" +
         "PROJECT = 'default'  # @param {type:\"string\"}\n" +
         "exec(" + repr((ROOT / "persistent_colab.py").read_text()) + ")\n" +
         "ws = Workspace(PROJECT)\nws.prepare()\nws.activate()\n" +
         "print('Previous saved state:', ws.read_checkpoint())\n"),
    cell("markdown", """## Work here

Your current directory is the durable Drive workspace. Use `ws.root / 'src'`, `'data'`, `'models'`, `'outputs'`, and `'checkpoints'` for files that must survive.

Install additional packages with `ws.install('package==version')`; it saves the environment immediately. The environment inherits Colab's preinstalled AI libraries. Do not use `%pip` or `!pip` for packages you want this environment to retain. Use `ws.run(code)` or `subprocess.run([str(ws.python), ...])` for an independent Python process and for packages already imported by the notebook kernel. Installing a replacement for an already loaded package requires restarting the kernel and running cells 1 and 2 again.

Hugging Face model downloads and Torch downloads use the Drive model directory. Authentication tokens stay on the temporary runtime. Keep secrets in Colab Secrets; never in the notebook, workspace or checkpoints.

For many small files, unpack a dataset on the runtime's local disk for speed, retaining its original archive on Drive. Only files actually saved back to Drive survive. Concurrent writers to the same project are unsupported.
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

System installations (`apt`), OS services and driver changes are not included in the Python snapshot. Put necessary setup commands in explicit notebook cells and rerun them on a new VM.
"""),
    cell("code", """# @title Save environment
saved_environment = ws.save_environment()
print('Saved at:', saved_environment['created_utc'])
"""),
    cell("markdown", """## Before releasing compute

Run the next cell **only when finished**. It flushes and unmounts Drive. Then disconnect and delete the runtime using the Runtime menu. To continue working after unmounting, mount Drive again.

If Colab reports that its Python/base image changed, restart with a fresh runtime, run cell 1 and instantiate `ws` using cell 2; after the compatibility error, run `ws.rebuild()` and `ws.activate()` in a separate cell. This explicitly reinstalls the recorded package versions and saves a new snapshot; previous snapshots remain on Drive. Reinstallation can fail when a package no longer supports the new image.
"""),
    cell("code", """# @title Flush Drive (run manually when finished)
# Uncomment when you are ready to release the runtime.
# ws.flush()
"""),
]
for index, item in enumerate(cells):
    item["id"] = f"persistent-colab-{index}"
    item["metadata"]["id"] = item["id"]
    if item["cell_type"] == "code":
        item["metadata"]["cellView"] = "form"

notebook = {"nbformat": 4, "nbformat_minor": 5,
            "metadata": {"colab": {"name": "PersistentColab.ipynb", "provenance": [],
                                    "private_outputs": True},
                         "kernelspec": {"name": "python3", "display_name": "Python 3"},
                         "language_info": {"name": "python"}},
            "cells": cells}
(ROOT / "PersistentColab.ipynb").write_text(json.dumps(notebook, indent=2) + "\n")
print("Built PersistentColab.ipynb")
