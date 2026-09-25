"""Cross-platform runtime contracts; all children are local, provider-free fixtures."""
import contextlib
import copy
import errno
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import lite
import platform_support as host
import streaming
import submission
from fixture_support import fake_cli
from test_lite_v2 import events

SCRIPTS = str(Path(__file__).resolve().parent)


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='worker space ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_fixture(self, body, **kwargs):
        return streaming.run(['run'], str(self.root), kwargs.pop('timeout', 5),
            kwargs.pop('prompt', 'task'), fake_cli(self.root, body), lambda _: False,
            progress_stream=False, **kwargs)

    def test_lock_is_nonblocking_across_processes_and_reusable(self):
        lock = self.root / 'state.lock'
        code = (f'import sys;sys.path.insert(0,{SCRIPTS!r})\nimport lite\n'
                'try:\n'
                f' with lite.file_lock(__import__("pathlib").Path({str(lock)!r})): print("acquired")\n'
                'except lite.Failure as e: print(e.code)\n')
        def probe():
            return subprocess.run([sys.executable, '-c', code], capture_output=True,
                                  text=True, timeout=5, check=True).stdout.strip()
        with lite.file_lock(lock):
            self.assertEqual(probe(), 'busy')
        self.assertEqual(probe(), 'acquired')
        self.assertTrue(lock.exists())

    def test_lock_released_on_exception_without_truncation(self):
        lock = self.root / 'state.lock'
        lock.write_bytes(b'keep this content')
        with self.assertRaisesRegex(ValueError, 'fixture'):
            with lite.file_lock(lock):
                raise ValueError('fixture')
        with lite.file_lock(lock):
            pass
        self.assertEqual(lock.read_bytes(), b'keep this content')

    def test_windows_lock_seeks_and_maps_only_contention(self):
        fake = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=0)
        from unittest.mock import Mock
        fake.locking = Mock()
        with tempfile.TemporaryFile() as handle, patch.object(host, 'WINDOWS', True), \
             patch.object(host, 'msvcrt', fake, create=True):
            handle.write(b'abc')
            host.lock_file(handle)
            self.assertEqual(handle.tell(), 0)
            fake.locking.assert_called_with(handle.fileno(), 1, 1)
            handle.seek(3)
            host.unlock_file(handle)
            self.assertEqual(handle.tell(), 0)
            fake.locking.assert_called_with(handle.fileno(), 0, 1)
            fake.locking.side_effect = OSError(errno.EACCES, 'locked')
            with self.assertRaises(BlockingIOError):
                host.lock_file(handle)
            fake.locking.side_effect = OSError(errno.EIO, 'storage failure')
            with self.assertRaises(OSError) as caught:
                host.lock_file(handle)
            self.assertNotIsInstance(caught.exception, BlockingIOError)

    def test_config_and_brief_use_utf8_with_optional_bom(self):
        path = self.root / '\uc124\uc815.json'
        data = copy.deepcopy(lite.BASE)
        data['projects']['C:\\Users\\\ud14c\uc2a4\ud2b8'] = 'provider/model'
        lite.save(path, data)
        self.assertIn('\ud14c\uc2a4\ud2b8', path.read_bytes().decode('utf-8'))
        self.assertEqual(lite.load(path), data)
        path.write_bytes(b'\xef\xbb\xbf' + path.read_bytes())
        self.assertEqual(lite.load(path), data)
        brief = self.root / '\uc791\uc5c5.txt'
        brief.write_text('GOAL: \ud55c\uae00 \uacbd\ub85c', encoding='utf-8-sig')
        self.assertEqual(lite.read_brief(brief), 'GOAL: \ud55c\uae00 \uacbd\ub85c')

    def test_json_output_works_with_legacy_ascii_stdout(self):
        code = (f'import sys;sys.path.insert(0,{SCRIPTS!r});import lite;'
                "lite.emit({'path':'\\ud55c\\uae00'})")
        proc = subprocess.run([sys.executable, '-c', code], capture_output=True,
            env={**os.environ, 'PYTHONIOENCODING': 'ascii', 'PYTHONUTF8': '0'}, timeout=5, check=True)
        self.assertEqual(json.loads(proc.stdout.decode('utf-8')), {'path': '\ud55c\uae00'})

    def test_explicit_python_wrapper_keeps_arguments_literal(self):
        path = self.root / 'wrapper.py'
        path.write_text('pass\n')
        args = ['run', '--dir', 'space & literal%PATH%!^', '--variant', '"quoted"']
        cmd = host.opencode_command(args, {'OPENCODE_WORKER_BIN': str(path), 'PATH': ''})
        self.assertEqual(cmd, [sys.executable, str(path.resolve()), *args])

    def test_npm_launcher_is_invoked_without_cmd_or_powershell(self):
        shim = self.root / 'opencode.cmd'
        shim.write_text('@exit /b 99\n')
        launcher = self.root / 'node_modules/opencode-ai/bin/opencode'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('// fixture\n')
        with patch.object(host, 'WINDOWS', True), patch.object(host.shutil, 'which', return_value='node.exe'):
            cmd = host.opencode_command(['--dir', 'repo & literal%PATH%!^'],
                {'OPENCODE_WORKER_BIN': str(shim), 'PATH': ''})
        self.assertEqual(cmd, ['node.exe', str(launcher), '--dir', 'repo & literal%PATH%!^'])

    def test_project_local_npm_launcher_is_resolved(self):
        shim = self.root / 'node_modules/.bin/opencode.cmd'
        shim.parent.mkdir(parents=True)
        shim.write_text('@exit /b 99\n')
        launcher = self.root / 'node_modules/opencode-ai/bin/opencode'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('// fixture\n')
        with patch.object(host, 'WINDOWS', True), patch.object(host.shutil, 'which', return_value='node.exe'):
            self.assertEqual(host.opencode_command([], {'OPENCODE_WORKER_BIN': str(shim)})[1], str(launcher))

    def test_arbitrary_batch_wrapper_is_rejected(self):
        shim = self.root / 'opencode.cmd'
        shim.write_text('@echo should not run\n')
        with patch.object(host, 'WINDOWS', True), self.assertRaisesRegex(OSError, 'Unsupported OpenCode wrapper'):
            host.opencode_command(['run', '&not-a-command'], {'OPENCODE_WORKER_BIN': str(shim)})

    def test_npm_without_node_fails_without_installing(self):
        shim = self.root / 'opencode.cmd'
        shim.write_text('@exit /b 99\n')
        launcher = self.root / 'node_modules/opencode-ai/bin/opencode'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('// fixture\n')
        with patch.object(host, 'WINDOWS', True), patch.object(host.shutil, 'which', return_value=None), \
             self.assertRaisesRegex(FileNotFoundError, 'requires node.exe'):
            host.opencode_command([], {'OPENCODE_WORKER_BIN': str(shim)})

    def test_launch_error_has_no_fabricated_session(self):
        env = {**os.environ, 'OPENCODE_WORKER_BIN': str(self.root / 'missing.exe')}
        with self.assertRaises(FileNotFoundError) as caught:
            streaming.run([], str(self.root), 2, 'task', env, lambda _: False, progress_stream=False)
        self.assertFalse(caught.exception.worker_launched)
        self.assertIsNone(caught.exception.worker_summary.session)

    def test_streaming_drains_large_stderr_and_multibyte_unterminated_stdout(self):
        value = '\ud55c\uae00' * 500
        event = {'type': 'text', 'sessionID': 's', 'part': {'text': value}}
        raw = json.dumps(event, ensure_ascii=False).encode('utf-8')
        body = ('import sys\n'
                "sys.stderr.buffer.write(b'x'*2000000);sys.stderr.flush()\n"
                f'data={raw!r}\n'
                'for i in range(0,len(data),7):\n'
                ' sys.stdout.buffer.write(data[i:i+7]);sys.stdout.flush()\n')
        rc, summary, stderr, _ = self.run_fixture(body)
        self.assertEqual(rc, 0)
        self.assertTrue(stderr)
        self.assertFalse(summary.invalid)
        self.assertEqual(summary.answer, value)

    def test_oversized_line_is_discarded_but_later_events_survive(self):
        body = ('import sys,json\n'
                "sys.stdout.write('x'*10000+'\\n')\n"
                f'for item in {events()!r}: print(json.dumps(item))\n')
        with patch.object(streaming, 'MAX_LINE', 1024):
            rc, summary, _, _ = self.run_fixture(body)
        self.assertEqual(rc, 0)
        self.assertTrue(summary.invalid)
        self.assertEqual(summary.session, 'fixture-session')

    def test_large_prompt_uses_stdin_without_pipe_deadlock(self):
        prompt = '\ud55c\uae00' * 100000
        body = ('import sys,json\n'
                f'assert len(sys.stdin.buffer.read())=={len(prompt.encode())}\n'
                f'for item in {events()!r}: print(json.dumps(item))\n')
        self.assertEqual(self.run_fixture(body, prompt=prompt)[0], 0)

    def test_closed_pipes_do_not_disable_deadline(self):
        body = 'import os,time\nos.close(1);os.close(2);time.sleep(30)\n'
        with self.assertRaises((TimeoutError, subprocess.TimeoutExpired)) as caught:
            self.run_fixture(body, timeout=0.5)
        self.assertEqual(caught.exception.timeout_kind, 'hard')
        self.assertTrue(caught.exception.worker_launched)

    def test_pipe_reader_is_bounded_and_releases_threads(self):
        # Raw in-memory streams exercise backpressure independent of the OS.
        reader = host.PipeReader(io.BytesIO(b'x' * 4000000), io.BytesIO(b'y' * 4000000))
        try:
            time.sleep(0.05)
            self.assertLessEqual(reader.items.qsize(), host.PipeReader.QUEUE_SIZE)
        finally:
            reader.close()
        self.assertTrue(all(not t.is_alive() for t in reader.threads))

    def test_io_failure_retains_observed_summary_and_does_not_retry(self):
        original = host.PipeReader.read
        def fail_after_session(reader, timeout):
            if getattr(reader, 'fixture_returned', False):
                raise OSError(errno.EIO, 'fixture read failure')
            batch = original(reader, timeout)
            if batch:
                reader.fixture_returned = True
            return batch
        with patch.object(host.PipeReader, 'read', fail_after_session), self.assertRaises(OSError) as caught:
            self.run_fixture("import json,time\nprint(json.dumps({'type':'text','sessionID':'s','part':{'text':'partial'}}),flush=True)\ntime.sleep(30)\n")
        self.assertTrue(caught.exception.worker_launched)
        self.assertEqual(caught.exception.worker_summary.answer, 'partial')

    def test_sdk_and_plugin_use_file_uris_with_unicode_spaces(self):
        sdk_root = self.root / '\ud55c\uae00 sdk'
        sdk = sdk_root / 'node_modules/@opencode-ai/plugin/dist/tool.js'
        sdk.parent.mkdir(parents=True)
        sdk.write_text('// fixture\n')
        env = {'OPENCODE_CONFIG_DIR': str(sdk_root),
               'OPENCODE_CONFIG_CONTENT': json.dumps({'agent': {'codex-worker': {'permission': {}}}})}
        submission.configure(env)
        self.assertEqual(env['CODEX_WORKER_TOOL_SDK'], sdk.resolve().as_uri())
        self.assertNotIn(' ', env['CODEX_WORKER_TOOL_SDK'])
        self.assertTrue(json.loads(env['OPENCODE_CONFIG_CONTENT'])['plugin'][0].startswith('file:'))

    @unittest.skipUnless(os.name == 'nt', 'Native Windows path case semantics')
    def test_windows_case_aliases_share_project_and_lock(self):
        project = self.root / 'MixedCase'
        project.mkdir()
        self.assertEqual(lite.canonical_project(str(project)), lite.canonical_project(str(project).swapcase()))
        self.assertEqual(lite.lock_path(str(project)), lite.lock_path(str(project).swapcase()))

    @unittest.skipUnless(os.name == 'nt', 'Native Windows npm execution')
    @unittest.skipUnless(shutil.which('node'), 'Node required for npm launcher fixture')
    def test_windows_npm_actual_argv_preserves_shell_metacharacters(self):
        shim = self.root / 'opencode.cmd'
        shim.write_text('@exit /b 99\n')
        launcher = self.root / 'node_modules/opencode-ai/bin/opencode'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('console.log(JSON.stringify(process.argv.slice(2)))\n')
        args = ['run', '--dir', str(self.root / '\ud55c\uae00 & literal%PATH%!^'), 'a"b']
        cmd = host.opencode_command(args, {**os.environ, 'OPENCODE_WORKER_BIN': str(shim)})
        result = subprocess.run(cmd, capture_output=True, encoding='utf-8', check=True, timeout=5)
        self.assertEqual(json.loads(result.stdout), args)

    @unittest.skipUnless(os.name == 'nt', 'Native Windows Job Object cleanup')
    def test_windows_timeout_reaps_descendant_after_leader_exits(self):
        pid_file = self.root / 'descendant.pid'
        child = f'import os,time\nopen({str(pid_file)!r},"w").write(str(os.getpid()))\ntime.sleep(30)'
        body = ('import subprocess,sys,time\n'
                f'subprocess.Popen([sys.executable,"-c",{child!r}])\n'
                'time.sleep(0.2)\n')
        with self.assertRaises(TimeoutError):
            self.run_fixture(body, timeout=2)
        self.assertTrue(pid_file.exists())
        self.assert_windows_process_dead(int(pid_file.read_text()))

    @unittest.skipUnless(os.name == 'nt', 'Windows handle close on forced controller exit')
    def test_windows_job_closes_when_controller_is_forcibly_terminated(self):
        pid_file = self.root / 'worker.pid'
        body = f'import os,time\nopen({str(pid_file)!r},"w").write(str(os.getpid()))\ntime.sleep(30)'
        env = fake_cli(self.root, body)
        code = (f'import sys,os;sys.path.insert(0,{SCRIPTS!r});import streaming;'
                f'streaming.run([], {str(self.root)!r}, 15, "task", os.environ.copy(), lambda _: False, progress_stream=False)')
        controller = subprocess.Popen([sys.executable, '-c', code], env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline and controller.poll() is None:
                time.sleep(0.02)
            self.assertTrue(pid_file.exists(), 'fixture did not start')
            pid = int(pid_file.read_text())
            controller.terminate()
            controller.communicate(timeout=5)
            self.assert_windows_process_dead(pid)
        finally:
            if controller.poll() is None:
                controller.kill()
            controller.communicate(timeout=5)

    def assert_windows_process_dead(self, pid):
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE, never os.kill(pid, 0) on Windows
        if not handle:
            self.assertEqual(ctypes.get_last_error(), 87)  # Already gone, not access denied.
            return
        try:
            self.assertEqual(api.WaitForSingleObject(handle, 5000), 0)
        finally:
            api.CloseHandle(handle)


if __name__ == '__main__':
    unittest.main()
