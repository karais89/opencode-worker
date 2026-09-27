"""Final local validation remains fresh after a bounded successful Git observation."""
import unittest

import verification


class VerificationTests(unittest.TestCase):
    def test_git_status_after_check_preserves_observed_pass(self):
        report = {'validation_commands': ['python -m unittest']}
        base = {'last_non_shell_activity': 1, 'malformed_output': False,
                'shell_commands': [
                    {'sequence': 2, 'command': 'python -m unittest', 'exit': 0,
                     'tool_status': 'completed', 'command_truncated': False},
                    {'sequence': 3, 'command': 'git status --short', 'exit': 0,
                     'tool_status': 'completed', 'command_truncated': False}]}
        self.assertEqual(verification.assess(report, base)['status'], 'observed_pass')
        base['shell_commands'][1]['command'] = 'git status --short && touch changed'
        self.assertEqual(verification.assess(report, base)['status'], 'unverified')
        base['shell_commands'][1]['command'] = 'git status --short'
        base['shell_commands'][1]['exit'] = 1
        self.assertEqual(verification.assess(report, base)['status'], 'unverified')


if __name__ == '__main__':
    unittest.main()
