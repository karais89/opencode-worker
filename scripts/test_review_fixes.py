"""PR #9 engine, evidence and migration regressions. No provider calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install
import lite
import verification


class BoundEngineTests(unittest.TestCase):
    COMMANDS = (['models'], ['resolve'], ['set-default', 'null'],
                ['set-project', 'null'], ['run', '--brief', 'missing.txt'])

    def test_each_engine_command_is_bound_and_rejects_other_engine(self):
        for engine, other in (('grok', 'opencode'), ('opencode', 'grok')):
            for command in self.COMMANDS:
                with self.subTest(engine=engine, command=command):
                    self.assertEqual(lite.parser(engine).parse_args(command).engine, engine)
                    self.assertEqual(lite.parser(engine).parse_args(
                        command + ['--engine', engine]).engine, engine)
                    for options in (['--engine', other], ['--engine=' + other],
                                    ['--engine', other, '--engine', engine]):
                        with contextlib.redirect_stderr(io.StringIO()):
                            with self.assertRaises(SystemExit) as error:
                                lite.parser(engine).parse_args(command + options)
                        self.assertEqual(error.exception.code, 2)

    def test_source_cli_keeps_both_engines_and_opencode_default(self):
        for command in self.COMMANDS:
            self.assertEqual(lite.parser().parse_args(command).engine, 'opencode')
            for engine in ('opencode', 'grok'):
                self.assertEqual(lite.parser().parse_args(
                    command + ['--engine', engine]).engine, engine)

    def test_opposite_engine_is_rejected_before_io(self):
        with patch.object(lite, 'canonical_project') as project:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    lite.main(['run', '--brief', 'missing', '--engine', 'opencode'],
                              bound_engine='grok')
        self.assertEqual(error.exception.code, 2)
        project.assert_not_called()

    def test_invalid_binding_and_shared_mode(self):
        with self.assertRaises(ValueError):
            lite.parser('other')
        for engine in ('opencode', 'grok'):
            args = lite.parser(engine).parse_args(['set-mode', 'off'])
            self.assertEqual((args.mode, args.engine), ('off', engine))


class WindowsWrapperEvidenceTests(unittest.TestCase):
    def evidence(self, command):
        return verification.assess({'validation_commands': [command]}, {
            'malformed_output': False, 'last_non_shell_activity': 0,
            'shell_commands': [{'command': command, 'command_truncated': False,
                                'tool_status': 'completed', 'exit': 0, 'sequence': 1}]})

    def test_windows_wrappers_never_supply_observed_pass(self):
        commands = [
            'cmd /c tests', 'CMD.EXE /c tests', 'cmd.com /c tests',
            'powershell -EncodedCommand ZQB4AGkAdAAgADAA',
            'powershell.exe -File check.ps1', 'pwsh -c tests',
            'PWSH.EXE -EncodedCommand ZQB4AGkAdAAgADAA',
            r'C:\Windows\System32\cmd.exe /c tests',
            r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -File check.ps1',
            r'"C:\Program Files\PowerShell\7\pwsh.exe" -File check.ps1',
            r"'C:\Program Files\PowerShell\7\pwsh.exe' -File check.ps1",
            'C:/Windows/System32/CMD.EXE /c tests',
            'bash.exe -c tests', '/bin/bash -c tests', 'sh -c tests',
            'check.bat', 'check.cmd', 'check.ps1',
            '%ComSpec% /c tests', '$env:ComSpec /c tests', 'p^wsh -c tests',
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(verification.direct_command(command))
                result = self.evidence(command)
                self.assertEqual((result['status'], result['passed']), ('unverified', 0))

    def test_direct_checks_remain_observed_and_bad_syntax_is_unverified(self):
        for command in ('python -m unittest', 'python3 -m pytest', 'git diff --check',
                        r'"C:\Program Files\Python\python.exe" -m unittest',
                        r'C:\Python\python.exe -m unittest'):
            with self.subTest(command=command):
                self.assertTrue(verification.direct_command(command))
                self.assertEqual(self.evidence(command)['status'], 'observed_pass')
        for command in ('', '   ', '"unfinished', 'python -m unittest && echo done'):
            with self.subTest(command=command):
                self.assertFalse(verification.direct_command(command))


class InstalledBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True, timeout=10)
        (self.repo / 'seed.txt').write_text('seed\n', encoding='utf-8')
        subprocess.run(['git', '-C', str(self.repo), 'add', 'seed.txt'], check=True, timeout=10)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'seed'],
                       check=True, timeout=10)
        with contextlib.redirect_stdout(io.StringIO()):
            install.install(self.home)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'writer_default': 'provider/open',
            'grok': {'default': 'grok-model', 'projects': {}}}), encoding='utf-8')
        self.env = dict(os.environ, XDG_CONFIG_HOME=str(self.root / 'config-home'))
        self.env.pop('OPENCODE_CONFIG_CONTENT', None)

    def command(self, engine, *args):
        return [sys.executable, str(self.home / 'skills' / (engine + '-worker') / 'scripts/lite.py'),
                '--project', str(self.repo), '--config', str(self.config), *args]

    def test_installed_defaults_cannot_be_switched_by_environment_or_flags(self):
        for engine, model, other in (('grok', 'grok-model', 'opencode'),
                                      ('opencode', 'provider/open', 'grok')):
            env = dict(self.env, WORKER_ENGINE=other)
            result = subprocess.run(self.command(engine, 'resolve'), env=env,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual((value['engine'], value['model']), (engine, model))
            result = subprocess.run(self.command(engine, 'run', '--brief', 'missing',
                                    '--engine', other), env=env,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 2)
            self.assertIn('invalid choice', result.stderr)

    def run_fake_engine(self, engine):
        report = dict(status='completed', changed=['answer.txt'],
                      validation=['fixture passed'], risk='none', validation_commands=[])
        if engine == 'grok':
            body = ('import json,os,pathlib,sys\n'
                    'assert os.environ["GROK_MEMORY"] == "0"\n'
                    'root=pathlib.Path(sys.argv[sys.argv.index("--cwd")+1])\n'
                    '(root/"answer.txt").write_text("done\\n")\n'
                    'print(json.dumps({"type":"text","data":' + repr(json.dumps(report)) + '}),flush=True)\n'
                    'print(json.dumps({"type":"end","stopReason":"end_turn","sessionId":"fixture"}),flush=True)\n')
        else:
            events = [
                {'type': 'tool_use', 'sessionID': 'fixture', 'part': {
                    'tool': 'edit', 'callID': 'edit', 'state': {'status': 'completed',
                    'input': {'filePath': 'answer.txt'}}}},
                {'type': 'tool_use', 'sessionID': 'fixture', 'part': {
                    'tool': 'codex_worker_submit_result', 'callID': 'submit', 'state': {
                    'status': 'completed', 'output': json.dumps({'accepted': True, 'report': report})}}},
                {'type': 'step_finish', 'sessionID': 'fixture', 'part': {
                    'id': 'end', 'reason': 'stop', 'tokens': {'input': 1, 'output': 1,
                    'reasoning': 0, 'total': 2, 'cache': {'read': 0, 'write': 0}}}},
            ]
            body = ('import json,pathlib,sys\nassert sys.argv[1] == "run"\n'
                    'root=pathlib.Path(sys.argv[sys.argv.index("--dir")+1])\n'
                    '(root/"answer.txt").write_text("done\\n")\nsys.stdin.read()\n'
                    'for event in ' + repr(events) + ': print(json.dumps(event),flush=True)\n')
        fake = self.root / 'fake.py'
        fake.write_text(body, encoding='utf-8')
        # A wrong backend fails rather than reaching any installed real CLI.
        env = dict(self.env, OPENCODE_WORKER_BIN=str(self.root / 'absent.exe'),
                   GROK_WORKER_BIN=str(self.root / 'absent.exe'))
        env['GROK_WORKER_BIN' if engine == 'grok' else 'OPENCODE_WORKER_BIN'] = str(fake)
        sdk = self.root / 'sdk' / 'node_modules/@opencode-ai/plugin/dist/tool.js'
        sdk.parent.mkdir(parents=True)
        sdk.write_text('// fixture SDK location only\n', encoding='utf-8')
        env['OPENCODE_CONFIG_DIR'] = str(self.root / 'sdk')
        brief = self.root / 'brief.txt'
        brief.write_text('GOAL: write answer.txt\nDONE WHEN: content is done', encoding='utf-8')
        result = subprocess.run(self.command(engine, 'run', '--brief', str(brief),
                                '--explicit', '--inactivity-timeout', '5'), env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual((value['engine'], value['status']), (engine, 'completed'))
        self.assertEqual(value['observed_changed'], ['answer.txt'])

    def test_installed_grok_runs_without_engine_flag(self):
        self.run_fake_engine('grok')

    def test_installed_opencode_runs_without_engine_flag(self):
        self.run_fake_engine('opencode')

    def next_source(self):
        source = self.root / 'next-source'
        shutil.copytree(install.SOURCE, source,
                        ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
        (source / 'VERSION').write_text('2.3.0\n', encoding='utf-8')
        return source

    def test_migrate_unmanaged_does_not_authorize_managed_replacement(self):
        source = self.next_source()
        target = self.home / 'skills/opencode-worker'
        original = (target / 'BUNDLE_VERSION').read_bytes()
        with patch.object(install, 'SOURCE', source), patch.object(install, 'VERSION', '2.3.0'):
            for dry_run in (False, True):
                with self.subTest(dry_run=dry_run):
                    with self.assertRaisesRegex(ValueError, '--replace'):
                        install.install(self.home, skill='opencode-worker',
                                        migrate_unmanaged=True, dry_run=dry_run)
            self.assertEqual((target / 'BUNDLE_VERSION').read_bytes(), original)
            self.assertFalse((self.home / 'worker-bundles/2.3.0').exists())
            self.assertFalse((self.home / 'worker-backups').exists())
            with contextlib.redirect_stdout(io.StringIO()):
                install.install(self.home, skill='opencode-worker', replace=True)
        self.assertEqual((target / 'BUNDLE_VERSION').read_text().strip(), '2.3.0')
        self.assertEqual((self.home / 'skills/grok-worker/BUNDLE_VERSION').read_bytes(), original)
        self.assertTrue((self.home / 'worker-bundles' / install.VERSION).is_dir())

    def test_mixed_managed_and_unmanaged_targets_require_both_flags(self):
        unmanaged = self.home / 'skills/grok-worker'
        shutil.rmtree(unmanaged)
        unmanaged.mkdir()
        (unmanaged / 'note.txt').write_text('keep\n', encoding='utf-8')
        source = self.next_source()
        with patch.object(install, 'SOURCE', source), patch.object(install, 'VERSION', '2.3.0'):
            for options, message in (({'replace': True}, '--migrate-unmanaged'),
                                     ({'migrate_unmanaged': True}, '--replace')):
                with self.subTest(options=options), self.assertRaisesRegex(ValueError, message):
                    install.install(self.home, **options)
            self.assertFalse((self.home / 'worker-bundles/2.3.0').exists())
            self.assertEqual((unmanaged / 'note.txt').read_text(), 'keep\n')
            with contextlib.redirect_stdout(io.StringIO()):
                install.install(self.home, replace=True, migrate_unmanaged=True)
        saved = list((self.home / 'worker-backups').iterdir())
        self.assertEqual(len(saved), 1)
        self.assertEqual((saved[0] / 'note.txt').read_text(), 'keep\n')
        self.assertTrue((unmanaged / 'SKILL.md').is_file())


if __name__ == '__main__':
    unittest.main()
