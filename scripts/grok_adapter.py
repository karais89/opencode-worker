"""One headless Grok Build session with bounded event and process handling."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

import platform_support

MAX_LINE = 4 * 1024 * 1024
MAX_ANSWER = 16 * 1024
SHELL_TOOL_NAMES = {'run_terminal_command', 'run_terminal_cmd', 'bash', 'shell',
                    'execute', 'run_command', 'terminal'}


class Summary:
    def __init__(self):
        self.answer = []
        self.answer_size = 0
        self.last_turn_answer = ''
        self.last_text_sequence = 0
        self.last_tool_sequence = 0
        self.session_id = None
        self.stop_reason = None
        self.usage = None
        self.cost_usd = None
        self.model_usage = None
        self.errors = []
        self.malformed = False
        self.end_seen = False
        self.tool_calls = 0
        self.tool_errors = 0
        self.pending_tools = {}
        self.shell_commands = []
        self.event_sequence = 0
        self.last_non_shell_activity = 0
        self.events = 0

    def consume(self, event):
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            self.malformed = True
            return False
        kind = event['type']
        if kind == 'available_commands':
            return False  # Startup inventory is not model or tool activity.
        if self.end_seen:
            self.malformed = True
            return False
        self.event_sequence += 1
        if kind == 'plan' or kind.startswith('auto_compact_'):
            pass  # Documented lifecycle activity; it is not completion evidence.
        elif kind == 'max_turns_reached':
            self.errors.append(kind)
        elif kind == 'thought':
            if not isinstance(event.get('data'), str):
                self.malformed = True
                return False
        elif kind == 'text':
            fragment = event.get('data')
            if not isinstance(fragment, str):
                self.malformed = True
                return False
            self.answer_size += len(fragment)
            if self.answer_size > MAX_ANSWER:
                self.malformed = True
            else:
                self.answer.append(fragment)
                self.last_text_sequence = self.event_sequence
        elif kind == 'usage':
            if not isinstance(event.get('usage'), dict):
                self.malformed = True
                return False
            if self.answer:
                self.last_turn_answer = ''.join(self.answer).strip()
                self.answer = []
                self.answer_size = 0
            self.usage = event['usage']
        elif kind == 'end':
            self.end_seen = True
            self.session_id = event.get('sessionId')
            self.stop_reason = event.get('stopReason')
            if isinstance(event.get('usage'), dict):
                self.usage = event['usage']
            self.cost_usd = event.get('total_cost_usd')
            self.model_usage = event.get('modelUsage')
            if not isinstance(self.session_id, str) or not self.session_id:
                self.malformed = True
        elif kind == 'tool_call':
            identity = event.get('toolCallId')
            if not isinstance(identity, str) or not identity or identity in self.pending_tools:
                self.malformed = True
                return False
            self.tool_calls += 1
            self.pending_tools[identity] = dict(name=event.get('toolName'), kind=event.get('kind'))
            self.last_tool_sequence = self.event_sequence
        elif kind == 'tool_call_update':
            identity = event.get('toolCallId')
            status = event.get('status')
            if not isinstance(identity, str) or identity not in self.pending_tools:
                self.malformed = True
                return False
            self.last_tool_sequence = self.event_sequence
            if status in ('completed', 'failed', 'cancelled', 'error'):
                tool = self.pending_tools.pop(identity)
                if status != 'completed':
                    self.tool_errors += 1
                if tool.get('kind') == 'execute' or tool.get('name') in SHELL_TOOL_NAMES:
                    output = event.get('rawOutput')
                    output = output if isinstance(output, dict) else {}
                    command = output.get('command')
                    if not isinstance(command, str):
                        command = ''
                    self.shell_commands.append(dict(command=command[:256],
                        command_truncated=len(command) > 256 or output.get('truncated') is True,
                        tool_status=status, exit=output.get('exit_code'),
                        sequence=self.event_sequence))
                    self.shell_commands = self.shell_commands[-10:]
                else:
                    self.last_non_shell_activity = self.event_sequence
        elif kind in ('error', 'permission_denied'):
            self.errors.append(kind)
        else:
            self.malformed = True
            return False
        self.events += 1
        return True

    def report(self):
        if self.last_text_sequence <= self.last_tool_sequence:
            return ''
        return ''.join(self.answer).strip() or self.last_turn_answer

    def public(self):
        return dict(session_id=self.session_id, stop_reason=self.stop_reason,
                    worker_tokens=self.usage, worker_cost_usd=self.cost_usd,
                    model_usage=self.model_usage, tool_calls=self.tool_calls,
                    tool_errors=self.tool_errors, pending_tools=len(self.pending_tools),
                    shell_commands=self.shell_commands,
                    last_non_shell_activity=self.last_non_shell_activity,
                    errors=self.errors,
                    malformed_output=self.malformed, event_count=self.events)


def git_snapshot(root):
    """Hash only paths already dirty against HEAD; keep preexisting edits distinct."""
    commands = (["git", "-C", root, "diff", "HEAD", "--name-only", "-z"],
                ["git", "-C", root, "ls-files", "--others", "--exclude-standard", "-z"])
    names = set()
    for command in commands:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if result.returncode:
            raise ValueError('Grok evidence requires a Git checkout with a HEAD commit.')
        names.update(os.fsdecode(value) for value in result.stdout.split(b'\0') if value)
    snapshot = {}
    for name in names:
        path = Path(root) / name
        if path.is_symlink():
            snapshot[name.replace('\\', '/')] = 'symlink:' + os.readlink(path)
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(chunk)
            snapshot[name.replace('\\', '/')] = digest.hexdigest()
        elif path.is_dir():
            snapshot[name.replace('\\', '/')] = 'directory'
        else:
            snapshot[name.replace('\\', '/')] = 'deleted'
    return snapshot


def changed_since(before, after):
    return sorted(name for name in before.keys() | after.keys()
                  if before.get(name) != after.get(name))


def run(root, prompt, model=None, hard_timeout=None, inactivity_timeout=300,
        env=None, progress_stream=None):
    """Launch once. Only recognized Grok JSON events refresh inactivity."""
    env = (os.environ.copy() if env is None else env.copy())
    env['GROK_MEMORY'] = '0'
    summary = Summary()
    process = reader = job = None
    launched = completed = False
    previous_sigterm = None
    sigterm_installed = False
    if progress_stream is None:
        progress_stream = sys.stderr

    def attach(error):
        error.worker_summary = summary
        error.worker_launched = launched
        return error

    try:
        if threading.current_thread() is threading.main_thread():
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            def interrupt(signum, frame):
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                raise KeyboardInterrupt('Grok Worker interrupted by SIGTERM')
            signal.signal(signal.SIGTERM, interrupt)
            sigterm_installed = True
        with tempfile.TemporaryDirectory(prefix='grok-worker-') as directory:
            brief_path = Path(directory) / 'prompt.txt'
            brief_path.write_text(prompt, encoding='utf-8')
            args = ['--cwd', root, '--prompt-file', str(brief_path),
                    '--output-format', 'streaming-json', '--permission-mode', 'auto',
                    '--no-plan', '--no-subagents']
            if model:
                args += ['--model', model]
            command = platform_support.grok_command(args, env)
            job = platform_support.create_job()
            process = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                **platform_support.process_options())
            launched = True
            if job:
                job.assign(process)
            reader = platform_support.PipeReader(process.stdout, process.stderr)
            started = time.monotonic()
            active_deadline = started + inactivity_timeout
            hard_deadline = None if hard_timeout is None else started + hard_timeout
            last_progress = started
            buffer = b''
            discard = False

            def check_deadline():
                now = time.monotonic()
                if hard_deadline is not None and now >= hard_deadline:
                    kind = 'hard'
                elif now >= active_deadline:
                    kind = 'inactivity'
                else:
                    return
                error = TimeoutError('Grok Worker ' + kind + ' timeout')
                error.timeout_kind = kind
                raise error

            while reader.active:
                check_deadline()
                for source, chunk in reader.read(0.25):
                    if source == 'stderr':
                        continue
                    segments = chunk.split(b'\n')
                    for index, segment in enumerate(segments):
                        if not discard:
                            if len(buffer) + len(segment) > MAX_LINE:
                                buffer = b''
                                discard = True
                                summary.malformed = True
                            else:
                                buffer += segment
                        if index < len(segments) - 1:
                            if buffer.strip() and not discard:
                                try:
                                    if summary.consume(json.loads(buffer)):
                                        active_deadline = time.monotonic() + inactivity_timeout
                                except (ValueError, UnicodeError):
                                    summary.malformed = True
                            buffer = b''
                            discard = False
                now = time.monotonic()
                if progress_stream and now - last_progress >= 60:
                    try:
                        progress_stream.write(f'[grok-worker] running {int(now-started)}s | events={summary.events} | tools={summary.tool_calls}\n')
                        progress_stream.flush()
                    except (OSError, ValueError):
                        pass
                    last_progress = now
            if buffer.strip() and not discard:
                try:
                    summary.consume(json.loads(buffer))
                except (ValueError, UnicodeError):
                    summary.malformed = True
            check_deadline()
            try:
                process_rc = process.wait(timeout=max(0.01, min(
                    [deadline - time.monotonic() for deadline in (hard_deadline, active_deadline)
                     if deadline is not None])))
            except subprocess.TimeoutExpired as error:
                error.timeout_kind = ('hard' if hard_deadline is not None and
                                      hard_deadline <= active_deadline else 'inactivity')
                raise
            completed = True
            return process_rc, summary
    except (TimeoutError, subprocess.TimeoutExpired, KeyboardInterrupt, OSError) as error:
        raise attach(error)
    finally:
        if process is not None and not completed:
            platform_support.terminate_tree(process, job)
        if job:
            try:
                job.close()
            except OSError:
                pass
        if reader:
            reader.close()
        elif process is not None:
            for stream in (process.stdout, process.stderr):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass
        if sigterm_installed:
            signal.signal(signal.SIGTERM, previous_sigterm)
