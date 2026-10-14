# Persistent Colab

## Purpose
Provide a general Python/AI workspace whose files and saved Python environment
survive the release of a managed Colab runtime. This does not keep a VM or live
process alive.

## Layout
- `persistent_colab.py`: Drive-backed workspace and local virtual environment.
- `build_notebook.py`: generates the self-contained `PersistentColab.ipynb`.
- `README.md`: operation and persistence boundaries.
- `tests/`: integrity and compatibility checks for environment snapshots.

## Operation
- The private Drive deployment is in `My Drive/PersistentColab` and contains
  the notebook, helper and README. Update existing files rather than duplicating
  the deployment; preserve notebook sharing and private-output settings.
- Persistent root: `/content/drive/MyDrive/PersistentColab/workspaces/<project>`.
- Runtime environment: `/content/persistent-colab/<project>/.venv`.
- Use a CPU runtime for setup/verification; request a GPU only for GPU work.
- Keep credentials and private runtime/Drive metadata out of Git.
- Snapshot versions are immutable; never prune user snapshots automatically.
- Run `python3 build_notebook.py` after changing the helper, then
  `python3 -m unittest discover -s tests`.
- Verify restoration in a genuinely new Colab VM after persistence changes.
- Verified with two separate CPU VMs: a saved Python package, output file and
  JSON checkpoint survived release. GPU/CUDA extensions need hardware-specific
  validation when introduced. Drive authorization may be required for a new VM.
