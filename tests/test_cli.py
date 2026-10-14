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
    def transport(self, receipt=None, error=False, mounted=True):
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
                    with tempfile.TemporaryDirectory() as root, \
                         patch('os.path.ismount', return_value=mounted):
                        replaced = code.replace("Path('/content/drive/MyDrive')", f"Path({root!r})")
                        replaced = replaced.replace("Path('/mnt/pc')", f"Path({root!r})")
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
    def __init__(self, project, workspaces):
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

    def test_run_timeout_defaults_to_session_length(self):
        run = cli.parser().parse_args(['run', '-s', 'test', '-f', 'job.py'])
        status = cli.parser().parse_args(['status', '-s', 'test'])
        self.assertEqual((run.timeout, status.timeout), (86400, 600))

    def test_storage_options_accept_either_position(self):
        before = cli.parser().parse_args(['--storage', 'rclone', '--mount-point', '/mnt/x', 'status', '-s', 't'])
        after = cli.parser().parse_args(['status', '-s', 't', '--storage', 'rclone'])
        default = cli.parser().parse_args(['status', '-s', 't'])
        self.assertEqual((before.storage, before.mount_point), ('rclone', '/mnt/x'))
        self.assertEqual((after.storage, after.mount_point), ('rclone', '/mnt/pc'))
        self.assertEqual(default.storage, 'colab-drive')
        for bad in ('relative', '/', '/content/drive/x', '/mnt/../etc'):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.parser().parse_args(['--mount-point', bad, 'status', '-s', 't'])

    def test_rclone_status_reads_mounted_workspace(self):
        args = cli.parser().parse_args(['--storage', 'rclone', 'status', '-s', 'test'])
        stdout = io.StringIO()
        with patch('subprocess.run', side_effect=self.transport()), redirect_stdout(stdout):
            self.assertEqual(cli.execute_remote(args, ['colab']), 0)
        self.assertTrue(json.loads(stdout.getvalue())['root'].endswith('/workspaces/default'))

    def test_unmounted_rclone_storage_is_failure(self):
        args = cli.parser().parse_args(['--storage', 'rclone', 'restore', '-s', 'test'])
        with patch('subprocess.run', side_effect=self.transport(mounted=False)), \
             redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'not mounted'):
                cli.execute_remote(args, ['colab'])

    def test_mount_rclone_validates_config_before_remote_action(self):
        with tempfile.TemporaryDirectory() as root:
            conf = Path(root) / 'rclone.conf'
            cases = [(None, 'not found'), ('[other]\ntype = drive\n', 'not defined'),
                     ('# Encrypted rclone configuration File\n\nRCLONE_ENCRYPT_V0:\nabc', 'Encrypted')]
            for content, message in cases:
                if content is not None:
                    conf.write_text(content)
                stderr = io.StringIO()
                with patch('colab_workspace_cli.official_command', return_value=['colab']), \
                     patch('subprocess.run') as run, redirect_stderr(stderr):
                    self.assertEqual(cli.main(['mount-rclone', '-s', 't', '--conf', str(conf), '--remote', 'gdrive:PC']), 1)
                run.assert_not_called()
                self.assertIn(message, stderr.getvalue())
                self.assertNotIn('RCLONE_ENCRYPT', stderr.getvalue())

    def test_mount_rclone_uploads_config_and_keeps_it_out_of_exec_code(self):
        with tempfile.TemporaryDirectory() as root:
            conf = Path(root) / 'rclone.conf'
            conf.write_text('[gdrive]\ntype = drive\ntoken = {"refresh_token": "secret-sentinel"}\n')
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                if command[1] == 'exec':
                    code = json.loads(Path(command[command.index('-f') + 1]).read_text())['cells'][0]['source']
                    self.assertNotIn('secret-sentinel', code)
                    self.assertIn("'gdrive:PersistentColab', '/mnt/data'", code)
                return subprocess.CompletedProcess(command, 0)
            with patch('colab_workspace_cli.official_command', return_value=['colab']), \
                 patch('subprocess.run', side_effect=run), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['mount-rclone', '-s', 't', '--conf', str(conf),
                                           '--remote', 'gdrive:PersistentColab', '--mount-point', '/mnt/data']), 1)
            self.assertEqual([c[1] for c in calls], ['upload', 'exec'])
            self.assertEqual(calls[0][4], str(conf))
            self.assertRegex(calls[0][5], r'^/tmp/colab-workspace-[0-9a-f]{32}\.conf$')

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
