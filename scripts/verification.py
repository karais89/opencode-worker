"""Correlate declared local checks with bounded, observed shell events.

This is not a test-output parser or an implementation reviewer. A successful
process exit cannot prove pass counts, test quality, or remote job completion.
"""
from pathlib import PurePath
import re
import shlex


def direct_command(command):
    """Conservatively exclude compound shells; false negatives stay unverified."""
    if re.search(r'[\n\r;&|<>`]|\$\(', command):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words:
        return False
    # A shell wrapper can conceal a compound command even without metacharacters.
    return PurePath(words[0]).name not in ('sh', 'bash', 'zsh', 'dash', 'fish', 'eval')


def assess(report, attempt):
    """Check only the commands explicitly declared by the worker, never prose.

    Old reports and unavailable/truncated/stale evidence remain usable but are
    labelled unverified. Only a declared, observed failing command blocks a
    completed result. No commands or model calls are made by this function.
    """
    declared = report.get('validation_commands', [])
    result = {'status': 'unverified', 'scope': 'declared_local_command_exits_only',
              'passed': 0, 'failed': 0, 'unverified': len(declared)}
    if not declared or attempt.get('malformed_output'):
        return result
    records = attempt.get('shell_commands', [])
    # Treat unknown tools and non-check shell commands as possible mutations.
    # This deliberately favours "unverified" over claiming an old pass is fresh.
    barrier = attempt.get('last_non_shell_activity')
    if type(barrier) is not int:
        barrier = None
    for record in records:
        sequence = record.get('sequence')
        if (record.get('command') not in declared or record.get('command_truncated')):
            if barrier is not None and type(sequence) is int:
                barrier = max(barrier, sequence)
    for command in declared:
        if not direct_command(command):
            continue
        matches = [r for r in records if r.get('command') == command
                   and not r.get('command_truncated')]
        if not matches:
            continue
        # Array order can change when an old terminal event is duplicated.
        # Require ordering evidence rather than mistaking that for a rerun.
        if any(type(r.get('sequence')) is not int for r in matches):
            continue
        record = max(matches, key=lambda r: r['sequence'])
        code = record.get('exit')
        if record.get('tool_status') == 'error' or (type(code) is int and code != 0):
            result['failed'] += 1
            result['unverified'] -= 1
        elif (type(code) is int and code == 0
              and record.get('tool_status') == 'completed' and barrier is not None
              and type(record.get('sequence')) is int and record['sequence'] > barrier):
            result['passed'] += 1
            result['unverified'] -= 1
    if result['failed']:
        result['status'] = 'failed'
    elif result['passed']:
        result['status'] = 'partial' if result['unverified'] else 'observed_pass'
    return result
