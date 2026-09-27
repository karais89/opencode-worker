"""Real Git regression fixtures: no provider calls, network or global settings."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import git_evidence


class GitEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.seed(self.root / 'repo')

    def git(self, root, *args):
        result = subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture',
                                 '-c', 'user.email=fixture@example.invalid', *args],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def seed(self, path):
        path.mkdir()
        self.git(path, 'init', '-q')
        (path / 'seed.txt').write_text('seed\n', encoding='utf-8')
        self.git(path, 'add', '.')
        self.git(path, 'commit', '-qm', 'seed')
        return path

    def snapshot(self):
        return git_evidence.git_snapshot(str(self.repo))

    def submodule(self):
        dependency = self.seed(self.root / 'dependency')
        commits = [self.git(dependency, 'rev-parse', 'HEAD')]
        for version in ('two', 'three'):
            (dependency / 'seed.txt').write_text(version + '\n', encoding='utf-8')
            self.git(dependency, 'commit', '-qam', version)
            commits.append(self.git(dependency, 'rev-parse', 'HEAD'))
        self.git(self.repo, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(dependency), 'dep')
        submodule = self.repo / 'dep'
        self.git(submodule, 'checkout', '-q', commits[0])
        self.git(self.repo, 'add', '.')
        self.git(self.repo, 'commit', '-qm', 'submodule baseline')
        return submodule, commits

    def test_content_changes_and_reverts_preserve_preexisting_state(self):
        self.assertEqual(self.snapshot(), {})
        target = self.repo / 'seed.txt'
        target.write_text('preexisting\n', encoding='utf-8')
        before = self.snapshot()
        self.assertEqual(before, self.snapshot())
        target.write_text('worker edit\n', encoding='utf-8')
        self.assertEqual(git_evidence.changed_since(before, self.snapshot()), ['seed.txt'])
        target.write_text('seed\n', encoding='utf-8')
        self.assertEqual(self.snapshot(), {})
        self.assertEqual(git_evidence.changed_since(before, self.snapshot()), ['seed.txt'])

    @unittest.skipIf(os.name == 'nt', 'Requires POSIX executable-bit changes')
    def test_mode_change_on_preexisting_dirty_file_is_observed(self):
        self.git(self.repo, 'config', 'core.filemode', 'true')
        target = self.repo / 'seed.txt'
        target.write_text('preexisting\n', encoding='utf-8')
        target.chmod(0o644)
        before = self.snapshot()
        target.chmod(0o755)
        self.assertEqual(git_evidence.changed_since(before, self.snapshot()), ['seed.txt'])

    @unittest.skipIf(os.name == 'nt', 'Requires POSIX executable-bit changes')
    def test_core_filemode_false_ignores_tracked_and_untracked_chmod(self):
        self.git(self.repo, 'config', 'core.filemode', 'false')
        paths = [self.repo / 'seed.txt', self.repo / 'new.txt']
        for path in paths:
            path.write_text('preexisting\n', encoding='utf-8')
            path.chmod(0o644)
        before = self.snapshot()
        for path in paths:
            path.chmod(0o755)
        self.assertEqual(git_evidence.changed_since(before, self.snapshot()), [])

    @unittest.skipIf(os.name == 'nt', 'Requires POSIX executable-bit changes')
    def test_untracked_mode_changes_but_other_permissions_do_not(self):
        self.git(self.repo, 'config', 'core.filemode', 'true')
        target = self.repo / 'new.txt'
        target.write_text('preexisting\n', encoding='utf-8')
        target.chmod(0o644)
        before = self.snapshot()
        target.chmod(0o600)
        self.assertEqual(git_evidence.changed_since(before, self.snapshot()), [])
        target.chmod(0o700)
        self.assertEqual(git_evidence.changed_since(before, self.snapshot()), ['new.txt'])

    def test_preexisting_dirty_submodule_pointer_change(self):
        submodule, commits = self.submodule()
        self.assertEqual(self.snapshot(), {})
        self.git(submodule, 'checkout', '-q', commits[1])
        before = self.snapshot()
        self.git(submodule, 'checkout', '-q', commits[2])
        self.assertEqual(git_evidence.changed_since(before, self.snapshot()), ['dep'])
        self.git(submodule, 'checkout', '-q', commits[0])
        self.assertEqual(self.snapshot(), {})

    def test_preexisting_submodule_file_changes_are_not_just_dirty_flags(self):
        submodule, _ = self.submodule()
        target = submodule / 'seed.txt'
        target.write_text('preexisting\n', encoding='utf-8')
        before = self.snapshot()
        self.assertEqual(set(before), {'dep'})
        target.write_text('worker edit\n', encoding='utf-8')
        self.assertEqual(git_evidence.changed_since(before, self.snapshot()), ['dep'])

    def test_preexisting_submodule_untracked_file_changes(self):
        submodule, _ = self.submodule()
        target = submodule / 'new.txt'
        target.write_text('preexisting\n', encoding='utf-8')
        before = self.snapshot()
        self.assertEqual(set(before), {'dep'})
        target.write_text('worker edit\n', encoding='utf-8')
        self.assertEqual(git_evidence.changed_since(before, self.snapshot()), ['dep'])

    def test_submodule_ignore_settings_do_not_hide_evidence(self):
        submodule, _ = self.submodule()
        self.git(self.repo, 'config', 'diff.ignoreSubmodules', 'all')
        self.git(self.repo, 'config', 'submodule.dep.ignore', 'all')
        before = self.snapshot()
        (submodule / 'seed.txt').write_text('worker edit\n', encoding='utf-8')
        self.assertEqual(git_evidence.changed_since(before, self.snapshot()), ['dep'])

    def test_deinitialized_submodule_does_not_fall_back_to_parent_checkout(self):
        submodule, commits = self.submodule()
        self.git(submodule, 'checkout', '-q', commits[1])
        self.git(self.repo, 'add', 'dep')
        self.git(self.repo, 'submodule', 'deinit', '-f', 'dep')
        before = self.snapshot()
        self.assertTrue(before['dep'].startswith('gitlink:uninitialized:'))
        (self.repo / 'seed.txt').write_text('parent edit\n', encoding='utf-8')
        self.assertEqual(git_evidence.changed_since(before, self.snapshot()), ['seed.txt'])

    def test_nested_submodule_dirty_contents_are_fingerprinted(self):
        submodule, _ = self.submodule()
        self.git(submodule, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q',
                 str(self.root / 'dependency'), 'nested')
        self.git(submodule, 'commit', '-qam', 'nested submodule')
        self.git(self.repo, 'commit', '-qam', 'new dep commit')
        self.assertEqual(self.snapshot(), {})
        target = submodule / 'nested/seed.txt'
        target.write_text('preexisting\n', encoding='utf-8')
        before = self.snapshot()
        target.write_text('worker edit\n', encoding='utf-8')
        self.assertEqual(git_evidence.changed_since(before, self.snapshot()), ['dep'])


if __name__ == '__main__':
    unittest.main()
