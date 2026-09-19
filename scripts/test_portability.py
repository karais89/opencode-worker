"""Extraction/portability regression: this copy must not depend on the original P005
checkout, user-specific absolute paths or removed Full/Lite reviewer/contract modules."""
import ast
import io
import json
import contextlib
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import worker as w

SCRIPTS = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS.parent
DEAD_MODULES = ('contracts', 'review_flow')
DEAD_FLAGS = ('--with-review', '--review-policy', '--max-corrections', '--correct-from',
              '--contract', '--change-kind', '--skip-review-reason', '--reviewer-model',
              '--validate-contract', 'set-reviewer-default')


class PortabilityTests(unittest.TestCase):
    def _sources(self):
        for path in sorted(SCRIPTS.glob('*.py')):
            if path.name.startswith('test_'):
                continue
            yield path, path.read_text(encoding='utf-8')

    def test_no_original_checkout_or_user_absolute_paths(self):
        pattern = re.compile(r'P005|/Users/[A-Za-z0-9._-]+|/home/[A-Za-z0-9._-]+')
        for path, text in self._sources():
            with self.subTest(path=path.name):
                self.assertIsNone(pattern.search(text),
                                  'user-specific or original-project path in ' + path.name)

    def test_no_dead_reviewer_or_contract_imports(self):
        for path, text in self._sources():
            tree = ast.parse(text)
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split('.')[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split('.')[0])
            with self.subTest(path=path.name):
                self.assertFalse(imported & set(DEAD_MODULES),
                                 path.name + ' imports removed legacy module')

    def test_assets_resolved_relative_to_skill_root(self):
        # The agent profile must load from this copy's assets/, never an external root.
        original = json.loads((SKILL_ROOT / 'assets/worker-agent.json').read_text())
        for read_only in (False, True):
            with self.subTest(read_only=read_only):
                env = w.worker_env(read_only=read_only)
                profile = json.loads(env['OPENCODE_CONFIG_CONTENT'])['agent']['codex-worker']
                # The authoring prompt and credential/secret denies come from this
                # copy's asset unchanged; writer mode only layers task permissions.
                self.assertEqual(profile['prompt'], original['prompt'])
                self.assertEqual(profile['permission']['read']['*.env'],
                                 original['permission']['read']['*.env'])
                self.assertEqual(profile['permission']['read']['*.env.*'],
                                 original['permission']['read']['*.env.*'])
                if not read_only:
                    self.assertEqual(profile['permission']['edit']['*.env'],
                                     original['permission']['edit']['*.env'])

    def test_removed_cli_surface_is_rejected(self):
        for flag in DEAD_FLAGS:
            with self.subTest(flag=flag):
                argv = ['worker', '--config', '/tmp/none.json', '--project', '.', 'run', flag]
                out = io.StringIO()
                with patch('sys.argv', argv), contextlib.redirect_stdout(out):
                    with self.assertRaises(SystemExit):
                        w.main()

    def test_no_reviewer_default_in_config_schema(self):
        with tempfile.TemporaryDirectory() as t:
            loaded = w.load(Path(t) / 'missing')
        self.assertNotIn('reviewer_default', loaded)
        self.assertNotIn('reviewer_invocations', w.BASE)
        self.assertFalse(hasattr(w, 'apply_contract_gate'))


if __name__ == '__main__':
    unittest.main()
