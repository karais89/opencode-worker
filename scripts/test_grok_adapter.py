"""Grok engine contracts using a fake CLI; no provider calls."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import grok_adapter
import lite
import submission
import verification


def event(kind, **fields):
    return json.dumps({"type": kind, **fields})


class GrokWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / 'seed.txt').write_text('seed\n', encoding='utf-8')
        subprocess.run(['git', '-C', str(self.root), 'add', 'seed.txt'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'seed'], check=True)
        self.brief = Path(self.temp.name) / 'brief.txt'
        self.brief.write_text('GOAL: write answer.txt\nDONE WHEN: its content is done', encoding='utf-8')
        self.default = Path(self.temp.name) / 'config' / 'config.json'
        self.patcher = patch.object(lite, 'DEFAULT_CONFIG', self.default)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.data = copy.deepcopy(lite.BASE)

    def args(self, **changes):
        values = dict(brief=str(self.brief), engine='grok', model=None, variant=None,
                      explicit=True, read_only=False, skill_dir=[], hard_timeout=None,
                      inactivity_timeout=2)
        values.update(changes)
        return SimpleNamespace(**values)

    def fake_cli(self, body):
        path = Path(self.temp.name) / 'grok-fixture.py'
        path.write_text(body, encoding='utf-8')
        return {'GROK_WORKER_BIN': str(path)}

    def test_engine_configuration_is_separate_and_default_is_opencode(self):
        self.data['writer_default'] = 'provider/open'
        self.data['grok']['default'] = 'grok-4.7'
        self.assertEqual(lite.resolve_route(self.data, str(self.root))[:2],
                         ('global', 'provider/open'))
        self.assertEqual(lite.resolve_grok_route(self.data, str(self.root)),
                         ('global', 'grok-4.7'))
        self.assertEqual(lite.parser().parse_args(['run', '--brief', str(self.brief)]).engine,
                         'opencode')

    def test_grok_setters_keep_opencode_route_and_validate_inventory(self):
        self.data['writer_default'] = 'provider/open'
        lite.save(self.default, self.data)
        with patch.object(lite, 'grok_model_inventory', return_value={'grok-4.7': []}):
            settings = SimpleNamespace(action='set-default', engine='grok', model='grok-4.7',
                                       variant=None, clear_variant=False)
            result = lite.update_config(settings, self.default, str(self.root))
        loaded = lite.load(self.default)
        self.assertEqual(result['model'], 'grok-4.7')
        self.assertEqual(loaded['writer_default'], 'provider/open')
        self.assertEqual(loaded['grok']['default'], 'grok-4.7')
        self.assertEqual(lite.resolve_route(loaded, str(self.root))[1], 'provider/open')
        with patch.object(lite, 'grok_model_inventory', return_value={'grok-4.7': []}):
            settings.model = 'unknown'
            with self.assertRaises(lite.Failure):
                lite.update_config(settings, self.default, str(self.root))
        self.assertEqual(lite.load(self.default)['grok']['default'], 'grok-4.7')

    def test_real_fake_process_preserves_git_evidence_and_usage(self):
        report = dict(status='completed', changed=['answer.txt'],
                      validation=['fixture passed'], risk='none', validation_commands=[])
        body = ('import json, pathlib, sys\n'
                'root=pathlib.Path(sys.argv[sys.argv.index("--cwd")+1])\n'
                'assert "--prompt-file" in sys.argv\n'
                'assert "--no-subagents" in sys.argv\n'
                '(root/"answer.txt").write_text("done\\n")\n'
                f'print(json.dumps({{"type":"text","data":{json.dumps(json.dumps(report))}}}), flush=True)\n'
                'print(json.dumps({"type":"end","stopReason":"end_turn",'
                '"sessionId":"fixture-session","usage":{"input_tokens":10,"output_tokens":5},'
                '"total_cost_usd":0.01,"modelUsage":{"grok-4.7":{"modelCalls":1}}}),flush=True)\n')
        with patch.dict(os.environ, self.fake_cli(body)):
            result = lite.run_worker(self.args(), self.data, str(self.root), self.default)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['observed_changed'], ['answer.txt'])
        self.assertEqual(result['observed_model'], 'grok-4.7')
        self.assertEqual(result['worker_tokens']['input_tokens'], 10)
        self.assertEqual(result['validation_evidence']['status'], 'unverified')

    def test_report_mismatch_and_denial_cannot_complete(self):
        report = dict(status='completed', changed=[], validation=['passed'], risk='none')
        summary = grok_adapter.Summary()
        summary.consume({'type': 'text', 'data': json.dumps(report)})
        summary.consume({'type': 'end', 'stopReason': 'end_turn', 'sessionId': 'fixture-session'})
        result = lite.grok_result_from_summary(0, summary, None, 'grok-cli-default', 1,
                                               {}, {'answer.txt': 'hash'})
        self.assertEqual(result['status'], 'needs_escalation')
        denied = grok_adapter.Summary()
        denied.consume({'type': 'text', 'data': json.dumps(report)})
        denied.consume({'type': 'permission_denied'})
        denied.consume({'type': 'end', 'stopReason': 'end_turn', 'sessionId': 'fixture-session'})
        self.assertEqual(lite.grok_result_from_summary(0, denied, None, 'grok-cli-default',
                         1, {}, {})['status'], 'needs_escalation')

    def test_usage_limit_is_reported_without_raw_provider_message(self):
        summary = grok_adapter.Summary()
        summary.consume({'type': 'error', 'message':
                         'You have reached your free Grok Build usage limit: private detail'})
        result = lite.grok_result_from_summary(1, summary, None, 'grok-cli-default',
                                               1, {}, {})
        self.assertEqual(result['status'], 'needs_escalation')
        self.assertIn('usage limit', result['message'])
        self.assertNotIn('private detail', result['message'])

    def test_only_final_turn_json_counts_and_terminal_exit_is_observed(self):
        summary = grok_adapter.Summary()
        summary.consume({'type': 'text', 'data': 'I will inspect the file.'})
        summary.consume({'type': 'usage', 'usage': {'input_tokens': 3}})
        summary.consume({'type': 'tool_call', 'toolCallId': 'edit-1',
                         'toolName': 'search_replace'})
        summary.consume({'type': 'tool_call_update', 'toolCallId': 'edit-1',
                         'status': 'completed'})
        summary.consume({'type': 'tool_call', 'toolCallId': 'check-1',
                         'toolName': 'run_terminal_command'})
        summary.consume({'type': 'tool_call_update', 'toolCallId': 'check-1',
                         'status': 'completed', 'rawOutput': {
                             'command': 'python -m unittest -v', 'exit_code': 0}})
        report = dict(status='completed', changed=['answer.txt'], validation=['passed'],
                      risk='none', validation_commands=['python -m unittest -v'])
        summary.consume({'type': 'text', 'data': json.dumps(report)})
        summary.consume({'type': 'usage', 'usage': {'input_tokens': 10}})
        summary.consume({'type': 'end', 'stopReason': 'end_turn', 'sessionId': 'session'})
        self.assertEqual(submission.validate(json.loads(summary.report())), report)
        self.assertEqual(verification.assess(report, summary.public())['status'],
                         'observed_pass')
        self.assertFalse(summary.malformed)
        self.assertEqual(len(summary.pending_tools), 0)

    def test_shell_alias_and_execute_kind_are_validation_evidence(self):
        for tool_name, tool_kind in (('run_terminal_cmd', None), ('future_shell_name', 'execute')):
            with self.subTest(tool_name=tool_name, tool_kind=tool_kind):
                summary = grok_adapter.Summary()
                call = {'type': 'tool_call', 'toolCallId': 'check',
                        'toolName': tool_name}
                if tool_kind:
                    call['kind'] = tool_kind
                summary.consume(call)
                summary.consume({'type': 'tool_call_update', 'toolCallId': 'check',
                                 'status': 'completed', 'rawOutput': {
                                     'command': 'python -m unittest -v', 'exit_code': 0}})
                self.assertEqual(summary.shell_commands[0]['exit'], 0)
                self.assertFalse(summary.malformed)

    def test_documented_lifecycle_events_are_not_malformed(self):
        summary = grok_adapter.Summary()
        for kind in (
                'plan', 'auto_compact_started', 'auto_compact_completed',
                'auto_compact_failed', 'auto_compact_cancelled',
                'auto_continue_completed', 'image_compressed',
                'memory_flush_started', 'memory_flush_completed',
                'memory_capture_activity'):
            self.assertTrue(summary.consume({'type': kind}))
        self.assertTrue(summary.consume({'type': 'max_turns_reached'}))
        self.assertFalse(summary.malformed)
        self.assertIn('max_turns_reached', summary.errors)

    def test_event_after_end_is_malformed_even_for_inventory_noise(self):
        summary = grok_adapter.Summary()
        self.assertTrue(summary.consume({
            'type': 'end', 'stopReason': 'end_turn', 'sessionId': 'session'}))
        self.assertFalse(summary.consume({
            'type': 'available_commands', 'tools': [], 'commands': []}))
        self.assertTrue(summary.malformed)

    def test_worker_disables_cross_session_grok_memory(self):
        body = ('import json, os\n'
                'assert os.environ.get("GROK_MEMORY") == "0"\n'
                'print(json.dumps({"type":"text","data":"{}"}), flush=True)\n'
                'print(json.dumps({"type":"end","stopReason":"end_turn",'
                '"sessionId":"fixture-session"}), flush=True)\n')
        supplied = self.fake_cli(body)
        supplied['GROK_MEMORY'] = '1'
        with patch.dict(os.environ, supplied):
            rc, summary = grok_adapter.run(str(self.root), 'task',
                                           inactivity_timeout=2, progress_stream=False)
        self.assertEqual(rc, 0)
        self.assertTrue(summary.end_seen)

    def test_inventory_noise_does_not_keep_silent_worker_alive(self):
        body = ('import json,time\n'
                'for _ in range(8):\n'
                ' print(json.dumps({"type":"available_commands","tools":[]}),flush=True)\n'
                ' time.sleep(0.1)\n'
                'time.sleep(2)\n')
        with patch.dict(os.environ, self.fake_cli(body)):
            with self.assertRaises(TimeoutError) as caught:
                grok_adapter.run(str(self.root), 'task', inactivity_timeout=0.2,
                                 progress_stream=False)
        self.assertEqual(caught.exception.timeout_kind, 'inactivity')

    def test_closed_pipes_still_report_hard_deadline(self):
        body = 'import os,time\nos.close(1);os.close(2);time.sleep(30)\n'
        with patch.dict(os.environ, self.fake_cli(body)):
            with self.assertRaises((TimeoutError, subprocess.TimeoutExpired)) as caught:
                grok_adapter.run(str(self.root), 'task', hard_timeout=0.25,
                                 inactivity_timeout=2, progress_stream=False)
        self.assertEqual(caught.exception.timeout_kind, 'hard')

    def test_git_delta_excludes_unchanged_preexisting_edits(self):
        (self.root / 'seed.txt').write_text('preexisting\n', encoding='utf-8')
        (self.root / 'unrelated.txt').write_text('keep\n', encoding='utf-8')
        before = grok_adapter.git_snapshot(str(self.root))
        (self.root / 'seed.txt').write_text('worker edit\n', encoding='utf-8')
        after = grok_adapter.git_snapshot(str(self.root))
        self.assertEqual(sorted(before), ['seed.txt', 'unrelated.txt'])
        self.assertEqual(grok_adapter.changed_since(before, after), ['seed.txt'])

    def test_grok_options_fail_before_launch(self):
        for options in ({'variant': 'high'}, {'read_only': True}, {'skill_dir': ['x']}):
            with self.subTest(options=options), self.assertRaises(lite.Failure):
                lite.run_worker(self.args(**options), self.data, str(self.root), self.default)

    def test_global_off_blocks_grok_before_launch(self):
        self.data['mode'] = 'off'
        with patch.object(lite.grok_adapter, 'run') as run:
            result = lite.run_worker(self.args(), self.data, str(self.root), self.default)
        self.assertEqual(result['status'], 'mode_blocked')
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
