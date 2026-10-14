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
- `colab_workspace_cli.py` and `pyproject.toml`: installed `colab-workspace`
  companion using the official `colab` executable as its transport.
- `skills/`: persistent-workspace skill and the official CLI's bundled skill.

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
- A separate Drive notebook, `CrossNotebookVerification.ipynb`, also restored
  the original project's package, file and checkpoint. A differently named
  project kept its file, checkpoint and saved-package directories separate,
  without changing the default project's snapshot. This is directory and
  environment separation, not a security boundary; use separate kernels when
  switching projects and one snapshot writer per project.
- The companion requires an explicit session name. It does not auto-allocate,
  auto-authorize Drive or change official credentials/session-state files.
- Remote commands use temporary notebooks and structured success receipts:
  official `colab exec` 0.7.4 can return zero after a remote Python error.
  `finish` must not stop a runtime until save and Drive flush confirm success.
- Install locally with `uv tool install --editable /Users/jamie/persistent-colab`.
  Installed skill directories in `~/.codex/skills` link to the tracked `skills/`.
- The installed companion was tested on a real CPU VM through official CLI
  0.7.4: Drive mount, existing snapshot restore, script execution/arguments,
  exit-code propagation, package installation, status, and save/flush/stop.
  The test VM was released. CLI protocol/release-gate tests use local transport
  fixtures; helper persistence checks remain independent.
