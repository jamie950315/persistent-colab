"""Check structured remote success and the release-after-save contract."""
import json
import io
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import colab_workspace_cli as cli


class CliTest(unittest.TestCase):
    def transport(self, receipt=None, error=False):
        def run(command):
            source = Path(command[command.index('-f') + 1])
            code = json.loads(source.read_text())['cells'][0]['source']
            # Execute the actual generated wrapper with a temporary fake mount,
            # so protocol tests need no account, VM or installation.
            namespace = {}
            captured = []
            display_module = types.ModuleType('IPython.display')
            display_module.display = lambda data, **kw: captured.append(data)
            with patch.dict(sys.modules, {'IPython.display': display_module}):
                if not error:
                    with tempfile.TemporaryDirectory() as root:
                        replaced = code.replace("Path('/content/drive/MyDrive')", f"Path({root!r})")
                        exec(replaced, namespace)
            outputs = [{'output_type': 'display_data', 'data': data, 'metadata': {}}
                       for data in captured] if receipt != 'missing' else []
            if error:
                outputs.append({'output_type': 'error', 'ename': 'RuntimeError',
                                'evalue': 'Save failed', 'traceback': []})
            source.with_name('operation_output.ipynb').write_text(json.dumps({'cells': [{'outputs': outputs}]}))
            return subprocess.CompletedProcess(command, 0)
        return run

    def test_official_zero_exit_without_receipt_is_failure(self):
        args = cli.parser().parse_args(['status', '-s', 'test'])
        with patch('subprocess.run', side_effect=self.transport('missing')):
            self.assertEqual(cli.execute_remote(args, ['colab']), 1)

    def test_read_only_status_and_structured_receipt(self):
        args = cli.parser().parse_args(['status', '-s', 'test', '-p', 'separate'])
        with patch('subprocess.run', side_effect=self.transport()):
            self.assertEqual(cli.execute_remote(args, ['colab']), 0)

    def test_failed_finish_never_stops_runtime(self):
        with patch('colab_workspace_cli.official_command', return_value=['colab']), \
             patch('subprocess.run', side_effect=self.transport(error=True)) as run:
            self.assertEqual(cli.main(['finish', '-s', 'test']), 1)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1], 'exec')

    def test_successful_finish_stops_exact_session_after_receipt(self):
        args = ['--auth', 'oauth2', '--config', '/tmp/state with spaces.json', 'finish', '-s', 'specific']
        with patch('colab_workspace_cli.official_command', return_value=['colab', '--auth', 'oauth2']), \
             patch('colab_workspace_cli.execute_remote', return_value=0), \
             patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(cli.main(args), 0)
        self.assertEqual(run.call_args.args[0], ['colab', '--auth', 'oauth2', 'stop', '-s', 'specific'])

    def test_script_source_arguments_are_python_data(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "job's file.py"
            path.write_text("import sys\nprint(sys.argv)\nraise SystemExit(7)\n")
            args = cli.parser().parse_args(['run', '-s', 'test', '-f', str(path), '--', "x'\n__import__('os')", '$(touch malicious)'])
            code = cli.remote_script(args, 'test-nonce')
            compile(code, 'generated-remote', 'exec')
            self.assertIn(repr(path.read_text()), code)
            self.assertEqual(args.script_args[1:], ["x'\n__import__('os')", '$(touch malicious)'])

    def test_run_streams_child_output_and_propagates_exit_code(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'job.py'
            path.write_text("import sys\nprint(sys.argv[1])\nprint('child stderr', file=sys.stderr)\nraise SystemExit(7)\n")
            helper = f"""from pathlib import Path
import sys
class Workspace:
    def __init__(self, project):
        self.root = Path({root!r})
        self.local = self.root
        self.python = Path(sys.executable)
    def prepare(self):
        pass
"""
            args = cli.parser().parse_args(['run', '-s', 'test', '-f', str(path), '--', "literal $(touch nope) apostrophe's"])
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch('colab_workspace_cli.helper_source', return_value=helper), \
                 patch('subprocess.run', side_effect=self.transport()), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(cli.execute_remote(args, ['colab']), 7)
            self.assertEqual(stdout.getvalue().strip(), "literal $(touch nope) apostrophe's")
            self.assertEqual(stderr.getvalue().strip(), 'child stderr')

    def test_notebook_never_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'notebook.ipynb'
            self.assertEqual(cli.main(['notebook', '-p', 'training', '-o', str(path)]), 0)
            original = path.read_bytes()
            notebook = json.loads(original)
            for cell in notebook['cells']:
                if cell['cell_type'] == 'code':
                    compile(''.join(cell['source']), cell['id'], 'exec')
            self.assertEqual(cli.main(['notebook', '-o', str(path)]), 1)
            self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
