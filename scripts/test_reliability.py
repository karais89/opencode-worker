"""Regression tests for thin, evidence-aware handoffs; no live provider calls."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import streaming
import submission
import verification
import worker as w


def tool(name='bash', command='pytest -q', code=0, call='check', status='completed'):
    return {'type': 'tool_use', 'sessionID': 'session-test', 'part': {
        'tool': name, 'callID': call, 'state': {'status': status,
        'input': {'command': command}, 'metadata': {'exit': code}}}}


def collect(*events):
    summary = streaming.Summary(w.safe_to_replay)
    for event in events:
        summary.consume(event)
    return summary


def report(commands=None, **fields):
    return {'status': 'completed', 'changed': ['app.py'],
            'validation': ['pytest -q: 2 passed'], 'risk': 'none',
            'validation_commands': ['pytest -q'] if commands is None else commands, **fields}


class VerificationTests(unittest.TestCase):
    def assess(self, *events, commands=None):
        return verification.assess(report(commands), collect(*events).public())

    def test_observed_pass_is_only_a_process_exit_not_test_counts(self):
        result = self.assess(tool())
        self.assertEqual(result['status'], 'observed_pass')
        self.assertEqual(result['scope'], 'declared_local_command_exits_only')
        self.assertEqual(result['passed'], 1)
        self.assertNotIn('tests_passed', result)

    def test_declared_failure_is_not_hidden_by_unrelated_success(self):
        result = self.assess(tool(code=1), tool(command='npm test', call='second'),
                             commands=['pytest -q', 'npm test'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual((result['failed'], result['passed']), (1, 1))

    def test_successful_rerun_supersedes_old_failure(self):
        result = self.assess(tool(code=1), tool(name='edit', call='fix'), tool(call='rerun'))
        self.assertEqual(result['status'], 'observed_pass')
        self.assertEqual(result['failed'], 0)

    def test_unrelated_development_failure_does_not_block_final_check(self):
        result = self.assess(tool(command='grep absent app.py', code=1, call='search'), tool())
        self.assertEqual(result['status'], 'observed_pass')

    def test_prose_is_never_independent_evidence(self):
        for prose in ('not run', '0 passed, 1 failed', '200 passed'):
            result = verification.assess(report([], validation=[prose]), {})
            self.assertEqual(result['status'], 'unverified')
            self.assertEqual(result['passed'], 0)

    def test_missing_declaration_is_unverified_for_legacy_reports(self):
        legacy = report()
        del legacy['validation_commands']
        self.assertEqual(verification.assess(legacy, collect(tool()).public())['status'], 'unverified')

    def test_compounds_and_shell_wrappers_stay_unverified(self):
        for command in ('pytest -q; true', 'pytest -q | cat', 'pytest -q && echo done',
                        'bash -lc "pytest -q"', 'sh scripts/check.sh', 'pytest $(echo -q)'):
            with self.subTest(command=command):
                self.assertEqual(self.assess(tool(command=command), commands=[command])['status'], 'unverified')

    def test_missing_or_boolean_exit_is_unverified(self):
        for code in (None, False, '0'):
            with self.subTest(code=code):
                self.assertEqual(self.assess(tool(code=code))['status'], 'unverified')

    def test_later_file_change_invalidates_old_pass(self):
        self.assertEqual(self.assess(tool(), tool(name='edit', call='edit'))['status'], 'unverified')

    def test_later_shell_or_unknown_tool_invalidates_old_pass(self):
        for event in (tool(command='python3 update.py', call='later'), tool(name='custom_mcp', call='later')):
            self.assertEqual(self.assess(tool(), event)['status'], 'unverified')

    def test_native_reads_after_check_do_not_invalidate_pass(self):
        self.assertEqual(self.assess(tool(), tool(name='read', call='read'))['status'], 'observed_pass')

    def test_late_running_shell_is_not_silently_ignored(self):
        self.assertEqual(self.assess(tool(), tool(command='python3 update.py', call='pending', status='running'))['status'], 'unverified')

    def test_duplicate_event_is_not_a_fresh_rerun_after_edit(self):
        original = tool()
        self.assertEqual(self.assess(original, tool(name='edit', call='edit'), original)['status'], 'unverified')

    def test_duplicate_old_event_does_not_supersede_newer_check(self):
        for first, second, expected in ((0, 1, 'failed'), (1, 0, 'observed_pass')):
            older = tool(code=first, call='older')
            self.assertEqual(self.assess(older, tool(code=second, call='newer'), older)['status'], expected)

    def test_malformed_stream_cannot_advertise_observed_pass(self):
        attempt = collect(tool()).public()
        attempt['malformed_output'] = True
        self.assertEqual(verification.assess(report(), attempt)['status'], 'unverified')

    def test_conflicting_terminal_event_is_invalid(self):
        self.assertTrue(collect(tool(), tool(code=1)).invalid)

    def test_truncated_or_evicted_command_is_unverified(self):
        summary = collect(tool(command='pytest -q' + 'x' * 300))
        self.assertEqual(verification.assess(report(), summary.public())['status'], 'unverified')
        summary = collect(tool(), *(tool(command='echo ' + str(i), call=str(i)) for i in range(11)))
        self.assertEqual(verification.assess(report(), summary.public())['status'], 'unverified')

    def test_remote_completion_not_inferred_from_tool_transport(self):
        result = verification.assess(report([]), collect(tool(name='unity_run_tests')).public())
        self.assertEqual(result['status'], 'unverified')

    def test_partial_evidence_is_explicit(self):
        result = self.assess(tool(), commands=['pytest -q', 'npm test'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['unverified'], 1)

    def test_validation_commands_schema_is_bounded_and_optional(self):
        for commands in (None, 'pytest', [''], [None], ['x' * 257], ['pytest'] * 2, [str(i) for i in range(11)]):
            with self.subTest(commands=commands), self.assertRaises(ValueError):
                submission.validate({**report(), 'validation_commands': commands})
        self.assertEqual(submission.validate(report([]))['validation_commands'], [])
        self.assertEqual(submission.validate(report())['validation_commands'], ['pytest -q'])

    def test_controller_blocks_failed_check_without_relaunch(self):
        result_report = report()
        summary = collect(tool(code=1))
        summary.consume({'type': 'tool_use', 'sessionID': 'session-test', 'part': {
            'tool': submission.TOOL, 'callID': 'submit', 'state': {'status': 'completed',
            'output': json.dumps({'accepted': True, 'report': result_report})}}})
        with tempfile.TemporaryDirectory() as directory:
            fake = {'status': 'completed_needs_review', 'attempts': [
                {'status': 'completed_needs_review', **summary.public()}]}
            with patch.object(w, 'run_worker', return_value=fake) as run:
                result = w.run_direct(SimpleNamespace(evidence_dir=directory), {}, directory + '/repo')
            self.assertEqual(run.call_count, 1)
            self.assertEqual(result['public_result']['status'], 'needs_escalation')
            self.assertEqual(result['public_result']['failure_kind'], 'validation_failed')
            self.assertEqual(result['public_result']['validation_evidence']['failed'], 1)

    def test_read_only_cannot_report_changed_files_as_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = {'status': 'completed_needs_review', 'attempts': [
                {'result_submission': {'report': report([]), 'error': None}}]}
            args = SimpleNamespace(evidence_dir=directory, read_only=True)
            with patch.object(w, 'run_worker', return_value=fake):
                out = w.run_direct(args, {}, directory + '/repo')['public_result']
            self.assertEqual(out['failure_kind'], 'read_only_scope_violation')


class RoutingAndPromptTests(unittest.TestCase):
    def test_fallback_opt_in_for_every_route_source_and_no_fallback_wins(self):
        for source in ('one-shot', 'project', 'global'):
            for use, deny, expected in ((False, False, 1), (True, False, 2), (True, True, 1)):
                with self.subTest(source=source, use=use, deny=deny), tempfile.TemporaryDirectory() as directory:
                    task = Path(directory) / 'brief'
                    task.write_text('Investigate files only')
                    args = SimpleNamespace(model='p/first' if source == 'one-shot' else None,
                        use_opencode_default=False, no_fallback=deny, use_fallbacks=use,
                        task_file=str(task), read_only=True, timeout=5, config=Path(directory) / 'config')
                    config = copy.deepcopy(w.BASE)
                    config.update(default='p/first', fallbacks=['p/second'])
                    if source == 'project': config['projects'][directory] = 'p/first'
                    def failed(*args):
                        return 1, collect({'type': 'error', 'error': {'name': 'ProviderAuthError'}}), False, None
                    with patch.object(w, 'CONFIG', Path(directory) / 'runtime/config'), patch.object(w.streaming, 'run', side_effect=failed) as run:
                        result = w.run_worker(args, config, directory)
                    self.assertEqual(run.call_count, expected)
                    self.assertEqual(len(result['attempts']), expected)

    def test_read_only_prompt_does_not_require_impossible_shell_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / 'brief'; task.write_text('Find affected modules')
            args = SimpleNamespace(model='p/model', use_opencode_default=False, no_fallback=True,
                task_file=str(task), read_only=True, timeout=5, config=Path(directory) / 'config')
            def execute(argv, cwd, timeout, prompt, env, *rest):
                self.assertIn('Read-only investigation', prompt)
                self.assertIn('file/line evidence', prompt)
                self.assertNotIn('run each relevant test suite', prompt)
                permission = json.loads(env['OPENCODE_CONFIG_CONTENT'])['agent']['codex-worker']['permission']
                self.assertEqual(permission['bash'], 'deny')
                return 0, collect({'type': 'step_finish', 'part': {'reason': 'stop'}}), False, None
            with patch.object(w, 'CONFIG', Path(directory) / 'runtime/config'), patch.object(w.streaming, 'run', side_effect=execute):
                w.run_worker(args, copy.deepcopy(w.BASE), directory)

    def test_generic_flash_inventory_keeps_version_filter_honest(self):
        inventory = ['p/deepseek-flash', 'p/deepseek-v4.1-flash', 'p/other']
        with tempfile.TemporaryDirectory() as directory:
            out = io.StringIO()
            argv = ['worker', '--config', directory + '/config', '--project', directory, 'models']
            with patch('sys.argv', argv), patch.object(w, 'models', return_value=inventory), contextlib.redirect_stdout(out):
                w.main()
            result = json.loads(out.getvalue())
            self.assertEqual(result['deepseek_flash'], inventory[:2])
            self.assertEqual(result['deepseek_v4_1_flash'], [inventory[1]])


class CorrectionContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name); self.repo = self.base / 'repo'; self.repo.mkdir()
        self.brief = self.base / 'brief'; self.original_brief = 'Preserve the save format and old user data.'
        self.brief.write_text(self.original_brief)
        self.config = self.base / 'config'
        self.config.write_text(json.dumps({'default': 'p/original', 'variants': {'p/original': 'max'}}))
        self.fake = {'status': 'completed_needs_review', 'attempts': [{
            'status': 'completed_needs_review', 'session_id': 's-original',
            'route': 'p/original', 'variant': 'max',
            'result_submission': {'report': report([]), 'error': None}}]}

    def call(self, fix=None, extra=(), runtime=None):
        argv = ['worker', '--config', str(self.config), '--project', str(self.repo), 'run',
                '--brief', str(self.brief), '--evidence-dir', str(self.base / 'evidence'), *extra]
        if fix: argv += ['--fix-from', str(fix)]
        out = io.StringIO()
        with patch('sys.argv', argv), patch.object(w, 'CONFIG', self.base / 'runtime/config'), \
             patch.object(w, 'run_worker', side_effect=runtime or (lambda *a: copy.deepcopy(self.fake))), \
             contextlib.redirect_stdout(out):
            w.main()
        return json.loads(out.getvalue())

    def original(self, extra=()):
        result = self.call(extra=extra)
        self.assertNotIn(self.original_brief, json.dumps(result))
        path = Path(result['evidence_dir']) / 'writer.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(path.read_text())['task_context']['brief'], self.original_brief)
        return path

    def test_correction_preserves_brief_route_variant_after_config_change(self):
        prior = self.original()
        self.brief.write_text('Observed crash for empty names; accept an empty name safely.')
        self.config.write_text(json.dumps({'default': 'p/new', 'variants': {'p/original': 'low'}, 'fallbacks': ['p/expensive']}))
        def execute(args, config, root):
            self.assertEqual(args._original_brief, self.original_brief)
            self.assertIn('empty names', Path(args.task_file).read_text())
            self.assertEqual(args.model, 'p/original'); self.assertEqual(args.variant, 'max')
            self.assertEqual(config['variants']['p/original'], 'max')
            self.assertTrue(args.no_fallback); self.assertFalse(args.use_fallbacks)
            return copy.deepcopy(self.fake)
        self.assertTrue(self.call(prior, runtime=execute)['correction_used'])

    def test_correction_works_after_default_is_cleared(self):
        prior = self.original(); self.config.write_text('{}')
        self.assertTrue(self.call(prior)['correction_used'])

    def test_original_absence_of_variant_is_preserved(self):
        self.fake['attempts'][0]['variant'] = None
        prior = self.original()
        def execute(args, config, root):
            self.assertIsNone(args.variant)
            self.assertNotIn('p/original', config['variants'])
            return copy.deepcopy(self.fake)
        self.call(prior, runtime=execute)

    def test_last_executed_route_not_old_default_is_pinned(self):
        self.fake['attempts'][0]['route'] = 'p/fallback-used'
        prior = self.original()
        def execute(args, config, root):
            self.assertEqual(args.model, 'p/fallback-used')
            return copy.deepcopy(self.fake)
        self.call(prior, runtime=execute)

    def test_explicit_conflicting_model_or_variant_does_not_spend_slot(self):
        prior = self.original()
        for flags in (('--model', 'p/other'), ('--variant', 'low')):
            with self.assertRaises(w.Failure): self.call(prior, extra=flags)
            self.assertFalse((prior.parent / 'head-fix-used').exists())

    def test_legacy_missing_brief_cannot_be_reconstructed(self):
        prior = self.original(); old = json.loads(prior.read_text()); del old['task_context']
        prior.write_text(json.dumps(old))
        with self.assertRaises(w.Failure): self.call(prior)
        self.assertFalse((prior.parent / 'head-fix-used').exists())

    def test_original_read_only_work_cannot_be_upgraded_to_writer(self):
        prior = self.original(); old = json.loads(prior.read_text()); old['task_context']['read_only'] = True
        prior.write_text(json.dumps(old))
        with self.assertRaises(w.Failure): self.call(prior)

    def test_permissions_preserved_and_not_expanded_by_correction(self):
        prior = self.original(extra=('--allow-command', 'custom-check --quick'))
        with self.assertRaises(w.Failure): self.call(prior, extra=('--allow-command', 'other-check'))
        def execute(args, config, root):
            self.assertEqual(args.allow_command, ['custom-check --quick'])
            return copy.deepcopy(self.fake)
        self.call(prior, runtime=execute)

    def test_actual_correction_prompt_contains_original_and_new_briefs(self):
        prior = self.original(); self.brief.write_text('Fix the empty-name crash.')
        def execute(args, config, root):
            def launched(argv, cwd, timeout, prompt, env, *rest):
                self.assertIn(self.original_brief, prompt)
                self.assertIn('Fix the empty-name crash.', prompt)
                self.assertNotIn('--session', argv)
                summary = collect({'type': 'tool_use', 'sessionID': 's', 'part': {
                    'tool': submission.TOOL, 'callID': 'submit', 'state': {'status': 'completed',
                    'output': json.dumps({'accepted': True, 'report': report([])})}}},
                    {'type': 'step_finish', 'sessionID': 's', 'part': {'reason': 'stop'}})
                return 0, summary, False, None
            with patch.object(w, 'validate_variant'), patch.object(submission, 'configure'), patch.object(w.streaming, 'run', side_effect=launched):
                return w.run_worker(args, config, root)
        # Use the real run_worker saved before the outer patch replaces it.
        original_run = w.run_worker
        def actual(args, config, root):
            with patch.object(w, 'run_worker', original_run): return execute(args, config, root)
        self.call(prior, runtime=actual)


if __name__ == '__main__':
    unittest.main()
