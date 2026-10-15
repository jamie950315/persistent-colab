"""Persistence commands using the public official Colab CLI as transport."""
from __future__ import annotations

import argparse
import configparser
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

import persistent_colab

VERSION = "0.1.0"


def project_name(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise argparse.ArgumentTypeError("Use 1-64 letters, digits, underscores or hyphens; start with a letter or digit.")
    return value


def positive_timeout(value):
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("Timeout must be a positive finite number.")
    return number


def mount_point(value):
    path = PurePosixPath(value)
    if not path.is_absolute() or path == PurePosixPath("/") or ".." in path.parts \
            or path.is_relative_to("/content/drive") or "\n" in value:
        raise argparse.ArgumentTypeError("Use an absolute runtime directory outside /content/drive, such as /mnt/pc.")
    return str(path)


def rclone_remote(value):
    # A named remote from the uploaded config, optionally followed by a path.
    if not re.fullmatch(r"[A-Za-z0-9_.+@][A-Za-z0-9_.+@ -]*:[^\r\n]*", value):
        raise argparse.ArgumentTypeError("Use NAME:path with a remote defined in the rclone config, such as gdrive:PersistentColabData.")
    return value


def validate_rclone_config(path, remote):
    """Check the local config without printing any of its contents."""
    if not path.is_file():
        raise ValueError(f"rclone config not found: {path}")
    text = path.read_text()
    if text.lstrip().startswith("# Encrypted rclone configuration File"):
        raise ValueError("Encrypted rclone configs need an interactive password; use a dedicated unencrypted config.")
    config = configparser.ConfigParser(interpolation=None)
    try:
        config.read_string(text)
    except configparser.Error:
        raise ValueError(f"rclone config could not be parsed: {path}") from None
    name = remote.split(":", 1)[0]
    if not config.has_section(name):
        raise ValueError(f"Remote {name!r} is not defined in {path}.")


def storage_arguments(parser, default):
    parser.add_argument("--storage", choices=("colab-drive", "rclone"),
                        default="colab-drive" if default else argparse.SUPPRESS,
                        help="Workspace storage: Colab Drive mount or an rclone mount (default: colab-drive)")
    parser.add_argument("--mount-point", type=mount_point,
                        default="/mnt/pc" if default else argparse.SUPPRESS,
                        help="Runtime rclone mount point (default: /mnt/pc)")


def space_argument(parser, default):
    parser.add_argument("--space", type=project_name,
                        default="default" if default else argparse.SUPPRESS,
                        help="Workspace space within the shared data root (default: default)")


def keep_count(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Keep at least one snapshot.")
    return number


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--version", action="version", version=f"colab-workspace {VERSION}")
    result.add_argument("--colab", default="colab", help="Official CLI executable (default: colab)")
    result.add_argument("--auth", choices=("oauth2", "adc"), help="Forward to the official CLI; otherwise preserve its default")
    result.add_argument("--config", help="Official CLI session-state file")
    result.add_argument("--client-oauth-config", help="Official CLI OAuth client configuration")
    storage_arguments(result, default=True)
    space_argument(result, default=True)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check the official CLI version and authenticated sessions (no allocation)")
    notebook = commands.add_parser("notebook", help="Generate a self-contained Colab notebook locally")
    notebook.add_argument("-p", "--project", type=project_name, default="default")
    notebook.add_argument("-o", "--output", type=Path, required=True)
    mount = commands.add_parser("mount", help="Run official colab drivemount interactively in your terminal")
    mount.add_argument("-s", "--session", required=True)
    rclone = commands.add_parser("mount-rclone", help="Upload an rclone config and mount a remote without a TTY")
    rclone.add_argument("-s", "--session", required=True)
    rclone.add_argument("--conf", type=Path, default=Path.home() / ".config" / "rclone" / "rclone.conf",
                        help="Local rclone config to upload (default: ~/.config/rclone/rclone.conf)")
    rclone.add_argument("--remote", type=rclone_remote, required=True, help="Shared data remote and folder, such as gdrive:PersistentColabData")
    rclone.add_argument("--mount-point", type=mount_point, default=argparse.SUPPRESS,
                        help="Runtime mount point (default: /mnt/pc)")
    rclone.add_argument("--timeout", type=positive_timeout, default=600,
                        help="Official remote execution timeout in seconds (default: 600)")
    for name, help_text in (
        ("restore", "Restore the latest compatible environment on an existing mounted VM"),
        ("install", "Install versioned packages and save the environment"),
        ("run", "Execute a local Python script in the workspace's own Python process"),
        ("save", "Save an immutable environment snapshot"),
        ("status", "Read workspace paths and saved snapshot metadata"),
        ("spaces", "List spaces and projects on an existing mounted VM"),
        ("rebuild", "Explicitly reinstall saved virtual-environment requirements"),
        ("rebuild-system", "Explicitly reinstall saved apt/system pip packages on a fresh image"),
        ("compact", "Compact the system layer chain; retain old layers until explicit prune"),
        ("prune", "List old snapshots and unreferenced system layers; delete only with --yes"),
        ("flush", "Flush and unmount storage without releasing the VM"),
        ("finish", "Save, flush storage, then release this named VM; stop only after success"),
    ):
        command = commands.add_parser(name, help=help_text)
        storage_arguments(command, default=False)
        command.add_argument("-s", "--session", required=True)
        if name != "spaces":
            command.add_argument("-p", "--project", type=project_name, default="default")
        # Official exec has a wall-clock deadline and accepts no unlimited value.
        # Long jobs default to Colab's maximum session length instead.
        default_timeout = 86400 if name == "run" else 600
        command.add_argument("--timeout", type=positive_timeout, default=default_timeout,
                             help=f"Official remote execution timeout in seconds (default: {default_timeout})")
        if name in ("restore", "install", "run", "save", "rebuild", "rebuild-system", "compact", "finish"):
            command.add_argument("--force-unlock", action="store_true",
                                 help="Take over a workspace lock left by a runtime that no longer exists")
        if name in ("restore", "install", "run", "save", "compact", "finish"):
            command.add_argument("--allow-base-drift", action="store_true",
                                 help="Restore despite changed base packages, then require pip check to pass")
        if name == "prune":
            command.add_argument("--keep", type=keep_count, required=True, help="Newest snapshots to keep")
            command.add_argument("--yes", action="store_true", help="Delete instead of a dry run")
        if name == "install":
            command.add_argument("packages", nargs="+")
        if name == "run":
            command.add_argument("-f", "--file", type=Path, required=True)
            command.add_argument("script_args", nargs=argparse.REMAINDER, help="Script arguments following --")
    for command in commands.choices.values():
        space_argument(command, default=False)
    return result


def official_command(args):
    executable = shutil.which(args.colab)
    if executable is None:
        raise RuntimeError("Official colab CLI not found. Install it with: uv tool install google-colab-cli")
    command = [executable]
    for option in ("auth", "config", "client_oauth_config"):
        value = getattr(args, option)
        if value is not None:
            command.extend(["--" + option.replace("_", "-"), value])
    return command


def helper_source():
    return Path(persistent_colab.__file__).read_text()


def storage_setup(args):
    if args.command == "mount-rclone":
        return ""
    if args.storage == "rclone":
        return f"""storage = Path({args.mount_point!r})
if not os.path.ismount(storage):
    raise RuntimeError('rclone storage is not mounted at ' + str(storage) + '. Run colab-workspace mount-rclone -s SESSION --remote NAME:path first.')
data_root = storage
"""
    return """storage = Path('/content/drive/MyDrive')
if not storage.is_dir():
    raise RuntimeError('Drive is not mounted. Run colab-workspace mount -s SESSION in an interactive terminal, mount Drive in the notebook, or use --storage rclone. Mount authorization is required on each new VM.')
data_root = storage / 'PersistentColabData'
"""


def remote_script(args, nonce):
    # Execute inside a function so the user's notebook namespace/imports stay intact.
    setup = """from pathlib import Path
import json, os, subprocess, sys, tempfile, threading
""" + storage_setup(args) + """exit_code = 0
def stream_process(command, cwd, env=None):
    process = subprocess.Popen(command, cwd=cwd, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    def forward(pipe, target):
        with pipe:
            for line in pipe:
                target.write(line)
                target.flush()
    workers = [threading.Thread(target=forward, args=(process.stdout, sys.stdout)),
               threading.Thread(target=forward, args=(process.stderr, sys.stderr))]
    for worker in workers:
        worker.start()
    code = process.wait()
    for worker in workers:
        worker.join()
    return code
"""
    # Give helper classes their own module-like globals to avoid overwriting user globals.
    helper = f"namespace = {{'__name__': 'persistent_colab_workspace'}}\nexec({helper_source()!r}, namespace)\n"
    project_root = f"data_root / 'spaces' / {args.space!r} / 'workspaces' / {getattr(args, 'project', 'default')!r}"
    if args.command == "status":
        operation = f"""root = {project_root}
latest = root / 'environment' / 'latest.json'
print(json.dumps({{'space': {args.space!r}, 'project': {args.project!r}, 'storage': {args.storage!r},
                  'data_root': str(data_root), 'root': str(root), 'exists': root.is_dir(),
                  'local_python': str(Path('/content/persistent-colab') / {args.space!r} / {args.project!r} / '.venv/bin/python'),
                  'snapshot': json.loads(latest.read_text()) if latest.exists() else None}}, indent=2))
"""
    elif args.command == "spaces":
        operation = """spaces_root = data_root / 'spaces'
spaces = []
if spaces_root.is_dir():
    for path in sorted(spaces_root.iterdir()):
        if path.is_dir():
            projects_root = path / 'workspaces'
            projects = []
            if projects_root.is_dir():
                projects = sorted(project.name for project in projects_root.iterdir() if project.is_dir())
            spaces.append({'space': path.name, 'projects': projects})
print(json.dumps({'storage': """ + repr(args.storage) + """, 'data_root': str(data_root), 'spaces': spaces}, indent=2))
"""
    elif args.command == "mount-rclone":
        operation = helper + f"namespace['mount_rclone']({args.remote!r}, {args.mount_point!r}, {args.staged_config!r})\n"
    elif args.command == "prune":
        operation = helper + f"namespace['prune_snapshots']({project_root}, {args.keep!r}, apply={args.yes!r})\n"
    elif args.command == "flush" and args.storage == "rclone":
        operation = helper + f"namespace['release_lock']({project_root})\nnamespace['flush_rclone']([{project_root}])\n"
    elif args.command == "flush":
        operation = helper + f"namespace['release_lock']({project_root})\nfrom google.colab import drive\ndrive.flush_and_unmount()\nprint('Drive writes flushed; Drive is unmounted.')\n"
    else:
        operation = helper + f"ws = namespace['Workspace']({args.project!r}, drive_root=data_root, session={args.session!r}, space={args.space!r})\n"
        if args.command == "rebuild":
            operation += f"ws.rebuild(force_unlock={args.force_unlock!r})\n"
        elif args.command == "rebuild-system":
            operation += f"ws.rebuild_system(force_unlock={args.force_unlock!r})\n"
        else:
            operation += f"ws.prepare(force_unlock={args.force_unlock!r}, allow_base_drift={args.allow_base_drift!r})\n"
            if args.command == "compact":
                operation += "ws.compact()\n"
            elif args.command == "install":
                if any(p.startswith("-") for p in args.packages):
                    raise ValueError("Pass package specifications, not pip options.")
                operation += f"""pip_command = [str(ws.python), '-u', '-m', 'pip', 'install', *{args.packages!r}]
pip_exit = stream_process(pip_command, cwd=ws.root)
if pip_exit:
    raise subprocess.CalledProcessError(pip_exit, pip_command)
ws.save_environment()
print('Packages installed; environment snapshot saved.')
"""
            elif args.command in ("save", "finish"):
                operation += "ws.save_environment()\n"
                if args.command == "finish":
                    operation += "ws.flush()\n"
            elif args.command == "run":
                if args.file.suffix != ".py":
                    raise ValueError("run requires a .py script; use official colab exec for notebooks.")
                source = args.file.read_text()
                script_args = args.script_args
                if script_args[:1] == ["--"]:
                    script_args = script_args[1:]
                operation += f"""with tempfile.TemporaryDirectory(dir=ws.local) as temporary:
    script = Path(temporary) / {args.file.name!r}
    script.write_text({source!r})
    child_env = os.environ.copy()
    child_env['PYTHONPATH'] = os.pathsep.join([str(ws.root / 'src'), str(ws.root)])
    exit_code = stream_process([str(ws.python), '-u', str(script), *{script_args!r}], cwd=ws.root, env=child_env)
"""
    operation += f"\nfrom IPython.display import display\ndisplay({{'application/json': {{'colab_workspace_nonce': {nonce!r}, 'exit_code': exit_code}}}}, raw=True)\n"
    code = setup + operation
    return "def _colab_workspace_operation():\n" + "".join("    " + line + "\n" for line in code.splitlines()) + "_colab_workspace_operation()\ndel _colab_workspace_operation\n"


def notebook_document(project, space="default"):
    sources = [
        ("markdown", """# Persistent Colab workspace

On a fresh VM, run mount and restore before installing packages, changing system
files or importing project dependencies, including the first use of a project.
This records the unmodified image baseline so saved changes and package rebuild
recipes are accurate. Keep the same SPACE and PROJECT to reuse files and saved
environments from the shared PersistentColabData root. Save application
checkpoints explicitly; use one project per VM and one writer per project. Use a
fresh VM to switch projects or spaces.

Save captures the virtual environment plus apt/system pip changes and files under
`/usr/local`, `/opt`, `/etc`, `/var/lib` and `/root/.local`, excluding volatile
paths, known credential locations and other mounts. Keep secrets in Colab Secrets;
arbitrary secrets in captured directories can still be archived. RAM, GPU memory,
processes and host GPU drivers do not persist.

System layers require the same base image. On a fresh changed image, explicitly
use `ws.rebuild_system()` to reinstall recorded apt/system pip packages and begin
a new chain, then `ws.rebuild()` for virtual-environment requirements. Arbitrary
files from the old image are not replayed; keep their setup in source control.
`ws.compact()` compacts layers; old layers remain until explicit pruning.
"""),
        ("code", "# @title Mount Google Drive\nfrom google.colab import drive\ndrive.mount('/content/drive')\n"),
        ("code", "# @title Restore workspace\n" + f"SPACE = {space!r}  # @param {{type:\"string\"}}\n" +
         f"PROJECT = {project!r}  # @param {{type:\"string\"}}\n" +
         f"exec({helper_source()!r})\nws = Workspace(PROJECT, space=SPACE)\nws.prepare()\nws.activate()\n"),
        ("code", "# Write your code here. Install packages with ws.install('package==version').\nprint('Persistent workspace:', ws.root)\n"),
        ("code", "# @title Save system and Python environments\nws.save_environment()\n"),
        ("code", "# @title Flush Drive when finished, then disconnect and delete runtime\nFLUSH_DRIVE = False  # @param {type:\"boolean\"}\nif FLUSH_DRIVE:\n    ws.flush()\n"),
    ]
    cells = []
    for index, (kind, source) in enumerate(sources):
        value = {"id": f"workspace-{index}", "cell_type": kind,
                 "metadata": {"id": f"workspace-{index}"}, "source": source.splitlines(keepends=True)}
        if kind == "code":
            value.update(execution_count=None, outputs=[])
        cells.append(value)
    return {"nbformat": 4, "nbformat_minor": 5,
            "metadata": {"colab": {"private_outputs": True, "provenance": []},
                         "kernelspec": {"name": "python3", "display_name": "Python 3"},
                         "language_info": {"name": "python"}}, "cells": cells}


def execute_remote(args, command):
    nonce = uuid.uuid4().hex
    code = remote_script(args, nonce)  # Validate/read local inputs before any remote action.
    with tempfile.TemporaryDirectory(prefix="colab-workspace-") as directory:
        path = Path(directory) / "operation.ipynb"
        document = {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": [
            {"id": "workspace-operation", "cell_type": "code", "metadata": {},
             "source": code, "outputs": [], "execution_count": None}]}
        path.write_text(json.dumps(document))
        result = subprocess.run(command + ["exec", "-s", args.session, "-f", str(path), "--timeout", str(args.timeout)])
        if result.returncode:
            return result.returncode
        output = path.with_name("operation_output.ipynb")
        # Official exec 0.7.4 can exit zero after a Python error; require a receipt.
        if output.exists():
            for cell in json.loads(output.read_text())["cells"]:
                for item in cell.get("outputs", []):
                    data = item.get("data", {}).get("application/json", {})
                    if isinstance(data, dict) and data.get("colab_workspace_nonce") == nonce:
                        code = data["exit_code"]
                        return 128 - code if code < 0 else code
        print("Remote operation did not confirm success. Runtime has been left running; inspect colab status/log before continuing.", file=sys.stderr)
        return 1


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "notebook":
            with args.output.open("x") as stream:
                json.dump(notebook_document(args.project, args.space), stream, indent=2)
                stream.write("\n")
            print(args.output.resolve())
            return 0
        command = official_command(args)
        if args.command == "doctor":
            for subcommand in ("version", "sessions"):
                result = subprocess.run(command + [subcommand])
                if result.returncode:
                    return result.returncode
            return 0
        if args.command == "mount-rclone":
            # Validate locally before any remote action; contents never enter exec code or logs.
            validate_rclone_config(args.conf, args.remote)
            args.staged_config = f"/tmp/colab-workspace-{uuid.uuid4().hex}.conf"
            upload = subprocess.run(command + ["upload", "-s", args.session, str(args.conf), args.staged_config])
            if upload.returncode:
                return upload.returncode
        if args.command == "mount":
            if not sys.stdin.isatty():
                raise RuntimeError("mount needs an interactive terminal for Google authorization. Mount Drive in the Colab notebook when operating through an agent.")
            return subprocess.run(command + ["drivemount", "-s", args.session]).returncode
        result = execute_remote(args, command)
        if result == 0 and args.command == "finish":
            return subprocess.run(command + ["stop", "-s", args.session]).returncode
        return result
    except (OSError, ValueError, RuntimeError) as error:
        print(f"colab-workspace: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Runtime was not released; inspect its status before continuing.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
