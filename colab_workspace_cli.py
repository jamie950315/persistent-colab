"""Persistence commands using the public official Colab CLI as transport."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
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


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--version", action="version", version=f"colab-workspace {VERSION}")
    result.add_argument("--colab", default="colab", help="Official CLI executable (default: colab)")
    result.add_argument("--auth", choices=("oauth2", "adc"), help="Forward to the official CLI; otherwise preserve its default")
    result.add_argument("--config", help="Official CLI session-state file")
    result.add_argument("--client-oauth-config", help="Official CLI OAuth client configuration")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check the official CLI version and authenticated sessions (no allocation)")
    notebook = commands.add_parser("notebook", help="Generate a self-contained Colab notebook locally")
    notebook.add_argument("-p", "--project", type=project_name, default="default")
    notebook.add_argument("-o", "--output", type=Path, required=True)
    mount = commands.add_parser("mount", help="Run official colab drivemount interactively in your terminal")
    mount.add_argument("-s", "--session", required=True)
    for name, help_text in (
        ("restore", "Restore the latest compatible environment on an existing mounted VM"),
        ("install", "Install versioned packages and save the environment"),
        ("run", "Execute a local Python script in the workspace's own Python process"),
        ("save", "Save an immutable environment snapshot"),
        ("status", "Read workspace paths and saved snapshot metadata"),
        ("rebuild", "Explicitly reinstall saved requirements on a fresh changed base image"),
        ("flush", "Flush and unmount Drive without releasing the VM"),
        ("finish", "Save, flush Drive, then release this named VM; stop only after success"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("-s", "--session", required=True)
        command.add_argument("-p", "--project", type=project_name, default="default")
        # Official exec has a wall-clock deadline and accepts no unlimited value.
        # Long jobs default to Colab's maximum session length instead.
        default_timeout = 86400 if name == "run" else 600
        command.add_argument("--timeout", type=positive_timeout, default=default_timeout,
                             help=f"Official remote execution timeout in seconds (default: {default_timeout})")
        if name == "install":
            command.add_argument("packages", nargs="+")
        if name == "run":
            command.add_argument("-f", "--file", type=Path, required=True)
            command.add_argument("script_args", nargs=argparse.REMAINDER, help="Script arguments following --")
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


def remote_script(args, nonce):
    # Execute inside a function so the user's notebook namespace/imports stay intact.
    setup = """from pathlib import Path
import json, os, subprocess, sys, tempfile, threading
if not Path('/content/drive/MyDrive').is_dir():
    raise RuntimeError('Drive is not mounted. Run colab-workspace mount -s SESSION in an interactive terminal, or mount Drive in the notebook. Mount authorization is required on each new VM.')
exit_code = 0
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
    if args.command == "status":
        operation = f"""root = Path('/content/drive/MyDrive/PersistentColab/workspaces') / {args.project!r}
latest = root / 'environment' / 'latest.json'
print(json.dumps({{'project': {args.project!r}, 'root': str(root), 'exists': root.is_dir(),
                  'local_python': str(Path('/content/persistent-colab') / {args.project!r} / '.venv/bin/python'),
                  'snapshot': json.loads(latest.read_text()) if latest.exists() else None}}, indent=2))
"""
    elif args.command == "flush":
        operation = "from google.colab import drive\ndrive.flush_and_unmount()\nprint('Drive writes flushed; Drive is unmounted.')\n"
    else:
        # Give helper classes their own module-like globals to avoid overwriting user globals.
        operation = f"namespace = {{'__name__': 'persistent_colab_workspace'}}\nexec({helper_source()!r}, namespace)\nws = namespace['Workspace']({args.project!r})\n"
        if args.command == "rebuild":
            operation += "ws.rebuild()\n"
        else:
            operation += "ws.prepare()\n"
            if args.command == "install":
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


def notebook_document(project):
    sources = [
        ("markdown", "# Persistent Colab workspace\n\nRun mount and restore before importing project packages. Files under `ws.root` and saved packages survive runtime release. RAM, GPU memory and processes do not. Save application checkpoints explicitly; use one writer per project.\n"),
        ("code", "# @title Mount Google Drive\nfrom google.colab import drive\ndrive.mount('/content/drive')\n"),
        ("code", "# @title Restore workspace\n" + f"PROJECT = {project!r}  # @param {{type:\"string\"}}\n" +
         f"exec({helper_source()!r})\nws = Workspace(PROJECT)\nws.prepare()\nws.activate()\n"),
        ("code", "# Write your code here. Install packages with ws.install('package==version').\nprint('Persistent workspace:', ws.root)\n"),
        ("code", "# @title Save Python environment\nws.save_environment()\n"),
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
                json.dump(notebook_document(args.project), stream, indent=2)
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
