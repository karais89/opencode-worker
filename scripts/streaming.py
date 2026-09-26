"""Bounded JSONL consumption; raw logs are opt-in and never returned inline."""
import json
import submission
import hashlib
import os
from pathlib import Path
import platform_support
import signal
import subprocess
import sys
import tempfile
import threading
import time

MAX_LINE = 4 * 1024 * 1024
PROGRESS_HEARTBEAT_SECONDS = 60
MAX_TRACKED_CALLS = 10000
EVENT_TYPES = ("step_start", "step_finish", "text", "reasoning", "tool_use", "error")

def _looks_like_validation(command):
    text=' '+str(command).lower()+' '
    hints=(' pytest ',' unittest ',' npm test ',' pnpm test ',' yarn test ',' cargo test ',
           ' go test ',' dotnet test ',' mvn test ',' gradle test ',' test ',' lint ',' check ',' build ')
    return any(hint in text for hint in hints)



class Summary:
    def __init__(self, replay_check):
        self.replay_check = replay_check
        self.safe = True
        self.prework = True
        self.errors = []
        self.last_finish = None
        self.denied = False
        self.tools = {}
        self.tool_status = {}
        self.tool_call_status = {}
        self.files = []
        self.commands = []
        self.command_history = {}
        self.pending_tools = set()
        self.command_sequence = 0
        self.activity_sequence = 0
        self.last_non_shell_activity = 0
        self.answer = ''
        self.session = None
        self.truncated = False
        self.invalid = False
        self.usage = {k:0 for k in ('input','output','reasoning','cache_read','cache_write','reported_total')}
        self.usage_counts = {k:0 for k in self.usage}
        self.usage_steps = {}
        self.usage_incomplete = False
        self.tool_calls = set()
        self.submission = submission.Capture()
        self.progress_phase = 'starting'
        self.progress_revision = 0
        self.validation_failed = False

    def _set_progress_phase(self, phase):
        if phase != self.progress_phase:
            self.progress_phase = phase
            self.progress_revision += 1

    def progress(self):
        errors=sum(v.get('error',0) for v in self.tool_status.values())
        return {'phase':self.progress_phase,'tool_calls':sum(self.tools.values()),
                'files_changed':len(self.files),'tool_errors':errors}

    def record_usage(self, event):
        part=event.get('part',{})
        identity=part.get('id') or part.get('messageID')
        tokens=part.get('tokens')
        if not isinstance(identity,str) or not isinstance(tokens,dict):
            self.usage_incomplete=True; return
        key=hashlib.sha256((str(event.get('sessionID',''))+'|'+identity).encode()).hexdigest()
        if key in self.usage_steps: return
        if len(self.usage_steps)>=10000:
            self.usage_incomplete=True;return
        self.usage_steps[key]=True
        cache=tokens.get('cache',{})
        if not isinstance(cache,dict): cache={}
        values={**{k:tokens.get(k) for k in ('input','output','reasoning')},
                'cache_read':cache.get('read'),'cache_write':cache.get('write'),
                'reported_total':tokens.get('total')}
        for field,value in values.items():
            if type(value) is int and 0<=value<=10**12:
                self.usage[field]+=value; self.usage_counts[field]+=1
            else: self.usage_incomplete=True

    def token_usage(self):
        return {'reported':{k:self.usage[k] if self.usage_counts[k] else None for k in self.usage},
                'observed_steps':len(self.usage_steps),
                'coverage':'partial_or_unavailable' if self.usage_incomplete or not self.usage_steps or self.invalid else 'observed_step_events',
                'source':'OpenCode step_finish; provider-reported, not independently billed usage'}

    def bad_line(self):
        self.safe = False
        self.invalid = True

    def consume(self, event):
        if not isinstance(event, dict):
            self.bad_line(); return False
        try:
            self.safe = self.safe and self.replay_check([event])
            kind = event.get('type')
            part = event.get('part', {})
            sid = event.get('sessionID')
            if kind not in EVENT_TYPES or not isinstance(part, dict):
                self.bad_line(); return False
            # Only startup errors may lack a session. Never borrow an earlier
            # session ID to turn an unbound event into completion evidence.
            if not (kind == 'error' and sid is None and self.session is None):
                if not isinstance(sid, str) or not sid.strip() or len(sid) > 200:
                    self.bad_line(); return False
                if self.session and self.session != sid:
                    self.bad_line(); return False
                self.session = sid
            self.prework = self.prework and kind in ('step_start', 'error')
            if kind == 'step_start':
                self.last_finish = None
            if kind == 'error':
                error = event.get('error', {})
                data = error.get('data', {})
                if len(self.errors) >= 32:
                    self.bad_line(); return False
                code=data.get('statusCode')
                if code is not None and (type(code) is not int or not 100<=code<=599):
                    self.bad_line(); return False
                self.errors.append({'type':'error', 'error': {
                    'name':str(error.get('name',''))[:100],
                    'data':{'statusCode':code, 'message':str(data.get('message',''))[:1000]}}})
                self._set_progress_phase('provider_error')
            elif kind == 'step_finish':
                self.record_usage(event)
                reason=part.get('reason')
                if reason not in ('stop','length','content-filter','tool-calls','error','other','unknown'):
                    self.bad_line(); return False
                self.last_finish = {'type':'step_finish','part':{'reason':reason}}
            elif kind in ('text', 'reasoning'):
                text = part.get('text', '')
                if not isinstance(text, str):
                    self.bad_line(); return False
                if kind == 'text':
                    self.answer = text[:4000]
                    self.truncated |= len(text) > 4000
            elif kind == 'tool_use':
                name = part.get('tool')
                call = part.get('callID') or part.get('id')
                state = part.get('state', {})
                if (not isinstance(name, str) or not name or len(name) > 100
                        or not isinstance(call, str) or not call or len(call) > 512
                        or not isinstance(state, dict)):
                    self.bad_line(); return False
                tool_status = state.get('status')
                if tool_status not in ('completed', 'error', 'running', 'pending'):
                    self.bad_line(); return False
                key = hashlib.sha256((sid + '|' + call).encode()).hexdigest()
                if key not in self.tool_calls:
                    if len(self.tool_calls) >= MAX_TRACKED_CALLS:
                        self.truncated = True
                        self.bad_line(); return False
                    self.tool_calls.add(key)
                    bucket = name if name in self.tools or len(self.tools) < 64 else 'other'
                    self.tools[bucket] = self.tools.get(bucket, 0) + 1
                previous_status = self.tool_call_status.get(key)
                if previous_status in ('completed', 'error') and previous_status != tool_status:
                    self.bad_line(); return False
                self.last_finish = None
                self.submission.consume(part, sid)
                self.activity_sequence += 1
                if name not in ('read', 'glob', 'grep', 'list', 'bash', submission.TOOL):
                    self.last_non_shell_activity = self.activity_sequence
                self.denied |= 'rejected permission' in str(state.get('error','')).lower()
                if tool_status in ('pending', 'running'):
                    self.pending_tools.add(key)
                else:
                    self.pending_tools.discard(key)
                if name in ('read','glob','grep','list') and self.progress_phase=='starting':
                    self._set_progress_phase('exploring')
                elif name == submission.TOOL and tool_status=='completed':
                    self._set_progress_phase('finishing')
                elif name not in ('read','glob','grep','list','bash',submission.TOOL) and self.progress_phase=='starting':
                    self._set_progress_phase('exploring')
                # Track nonterminal calls too: stop + submission is insufficient
                # when another observed tool has not completed.
                if tool_status in ('completed', 'error') and previous_status != tool_status:
                    bucket = name if name in self.tools else 'other'
                    states = self.tool_status.setdefault(bucket, {'completed': 0, 'error': 0})
                    states[tool_status] += 1
                self.tool_call_status[key] = tool_status
                inp = state.get('input', {})
                if name in ('edit','write','patch','apply_patch') and state.get('status')=='completed':
                    self._set_progress_phase('fixing' if self.validation_failed else 'implementing')
                    path = inp.get('filePath', inp.get('path'))
                    if isinstance(path,str) and path not in self.files:
                        if len(self.files)<20: self.files.append(path[:256])
                        else: self.truncated=True
                if name == 'bash' and tool_status not in ('completed','error'):
                    self.last_non_shell_activity = self.activity_sequence
                if name == 'bash' and tool_status in ('completed','error'):
                    meta=state.get('metadata',{})
                    exit_code=meta.get('exit')
                    command=str(inp.get('command',''))
                    self.command_sequence += 1
                    identity=key or ('anonymous-'+str(self.command_sequence))
                    record={'command':command[:256], 'command_truncated':len(command)>256,
                            'tool_status':tool_status,
                            'exit':exit_code if type(exit_code) is int else None,
                            '_call':identity, 'sequence':self.activity_sequence}
                    # Keep identity/order separately from the public 10-command
                    # tail. Evicted duplicate events cannot become new checks.
                    fingerprint = hashlib.sha256(json.dumps(
                        [command, tool_status, record['exit']], ensure_ascii=True).encode()).hexdigest()
                    previous = self.command_history.get(identity)
                    if previous is not None:
                        if previous != fingerprint:
                            self.bad_line(); return False
                    else:
                        self.command_history[identity] = fingerprint
                        self.commands.append(record)
                        self.commands = self.commands[-10:]
                    if _looks_like_validation(command):
                        if record['exit'] not in (None,0) or tool_status=='error':
                            self.validation_failed=True
                            self._set_progress_phase('validation_failed')
                        else:
                            self.validation_failed=False
                            self._set_progress_phase('validating')
                    elif self.progress_phase=='starting':
                        self._set_progress_phase('exploring')

            return True
        except (TypeError, AttributeError, ValueError):
            self.bad_line()
            return False

    def classification_events(self):
        result=list(self.errors)
        if self.last_finish: result.append(self.last_finish)
        if self.denied: result.append({'type':'tool_use','part':{'state':{'error':'rejected permission'}}})
        return result

    def public(self):
        return {'result_submission':self.submission.public(), 'session_id':self.session, 'tools':self.tools, 'tool_status':self.tool_status, 'worker_tokens':self.token_usage(),
                'last_non_shell_activity':self.last_non_shell_activity, 'pending_tools':len(self.pending_tools),
                'mutation_files_reported_by_tools':self.files, 'shell_commands':[{k:v for k,v in x.items() if k!='_call'} for x in self.commands],
                'provider_errors':[{'name':e['error']['name'],'status_code':e['error']['data']['statusCode']} for e in self.errors[:5]], 'worker_answer':self.answer, 'summary_truncated':self.truncated,
                'replay_safe':self.safe, 'malformed_output':self.invalid}

def run(args, cwd, timeout, prompt, env, replay_check, log_dir=None, progress_stream=None, progress_heartbeat=PROGRESS_HEARTBEAT_SECONDS, inactivity_timeout=None):
    # timeout is an optional overall wall-clock hard limit (None disables it).
    # inactivity_timeout interrupts only after this many seconds without a
    # consumed OpenCode JSON event. Controller heartbeat output never counts.
    summary=Summary(replay_check)
    log=None; log_path=None
    if log_dir:
        directory=Path(log_dir).expanduser().resolve(); directory.mkdir(parents=True,exist_ok=True)
        fd, name=tempfile.mkstemp(prefix='worker-',suffix='.jsonl',dir=directory)
        log=os.fdopen(fd,'wb'); log_path=name
    p=None; completed=False; launched=False
    reader=None; job=None
    previous_sigterm = None
    sigterm_installed = False
    if progress_stream is False:
        progress_stream=None
    elif progress_stream is None:
        progress_stream=sys.stderr
    def attach(error):
        # Observed evidence must survive any raised error, including cleanup/OSError.
        summary.usage_incomplete=True
        error.worker_summary=summary
        error.worker_log=log_path
        error.worker_launched=launched
        return error
    try:
        if threading.current_thread() is threading.main_thread():
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            def interrupt(signum, frame):
                # Ignore repeated termination requests while finally cleans up.
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                raise KeyboardInterrupt('Worker interrupted by SIGTERM')
            signal.signal(signal.SIGTERM, interrupt)
            sigterm_installed = True
        # A temporary stdin file prevents pipe-write deadlock on large task prompts.
        with tempfile.TemporaryFile() as stdin:
            stdin.write(prompt.encode()); stdin.seek(0)
            command=platform_support.opencode_command(args, env)
            job=platform_support.create_job()
            p=subprocess.Popen(command,cwd=cwd,env=env,stdin=stdin,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0,
                **platform_support.process_options())
            launched=True
            if job: job.assign(p)
            started=time.monotonic(); last_event_at=None; last_progress_at=started
            last_progress_revision=summary.progress_revision
            def show_progress(force=False):
                nonlocal last_progress_at,last_progress_revision
                if not progress_stream: return
                now=time.monotonic()
                changed=summary.progress_revision!=last_progress_revision
                heartbeat=bool(progress_heartbeat) and now-last_progress_at>=progress_heartbeat
                if not (force or changed or heartbeat): return
                snap=summary.progress()
                parts=[f"[opencode-worker] running {int(now-started)}s",
                       f"phase={snap['phase']}",f"tools={snap['tool_calls']}",
                       f"files={snap['files_changed']}"]
                if snap['tool_errors']: parts.append(f"tool_errors={snap['tool_errors']}")
                if heartbeat and not changed:
                    if last_event_at is None: parts.append("last_event=none")
                    else: parts.append(f"last_event={int(now-last_event_at)}s_ago")
                try:
                    progress_stream.write(' | '.join(parts)+'\n'); progress_stream.flush()
                except (OSError,ValueError):
                    pass
                last_progress_at=now; last_progress_revision=summary.progress_revision
            show_progress(force=True)
            reader=platform_support.PipeReader(p.stdout,p.stderr)
            buffer=b''; discard=False; stderr_present=False
            start=time.monotonic()
            hard_deadline=None if timeout is None else start+timeout
            active_deadline=None if inactivity_timeout is None else start+inactivity_timeout
            def deadline_reached():
                # Hard wall-clock limit first, then the inactivity deadline.
                for limit,kind in ((hard_deadline,'hard'),(active_deadline,'inactivity')):
                    if limit is not None and time.monotonic()>=limit: return kind
                return None
            def select_window():
                limits=[x for x in (hard_deadline,active_deadline) if x is not None]
                if not limits: return 0.25
                return min(0.25,max(0,min(limits)-time.monotonic()))
            def wait_timeout():
                limits=[x for x in (hard_deadline,active_deadline) if x is not None]
                if not limits: return None
                return max(.01,min(limits)-time.monotonic())
            def mark_activity():
                # Only consumed OpenCode JSON events refresh inactivity. Progress
                # heartbeat output and stderr are never Worker activity.
                nonlocal last_event_at,active_deadline
                last_event_at=time.monotonic()
                if inactivity_timeout is not None:
                    active_deadline=last_event_at+inactivity_timeout
            while reader.active:
                kind=deadline_reached()
                if kind:
                    error=TimeoutError('Worker inactivity timeout' if kind=='inactivity' else 'Worker timeout')
                    error.timeout_kind=kind
                    raise error
                for name,chunk in reader.read(select_window()):
                    if name=='stderr':
                        stderr_present=True; continue
                    if log: log.write(chunk)
                    # Split bounded chunks; never accumulate an unbounded JSON line.
                    segments=chunk.split(b'\n')
                    for i, segment in enumerate(segments):
                        if not discard:
                            if len(buffer)+len(segment)>MAX_LINE:
                                buffer=b''; discard=True; summary.bad_line()
                            else: buffer+=segment
                        if i<len(segments)-1:
                            if not discard and buffer.strip():
                                try:
                                    if summary.consume(json.loads(buffer)):
                                        mark_activity()
                                    show_progress()
                                except (ValueError,UnicodeError): summary.bad_line()
                            buffer=b''; discard=False
                show_progress()
            if buffer.strip() and not discard:
                try:
                    if summary.consume(json.loads(buffer)):
                        mark_activity()
                    show_progress()
                except (ValueError,UnicodeError): summary.bad_line()
            try:
                rc=p.wait(timeout=wait_timeout())
            except subprocess.TimeoutExpired as error:
                error.timeout_kind=(deadline_reached() or
                    ('hard' if hard_deadline is not None and
                     (active_deadline is None or hard_deadline<=active_deadline)
                     else 'inactivity'))
                raise
            completed=True
            summary._set_progress_phase('process_exited')
            show_progress(force=True)
            return rc,summary,stderr_present,log_path
    except (TimeoutError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        raise attach(error)
    except OSError as error:
        # Launched-process I/O failures after execution started must not be
        # relabelled as launch_failed; they carry observed session evidence.
        raise attach(error)
    finally:
        if p is not None and not completed:
            platform_support.terminate_tree(p,job)
        if job:
            try: job.close()
            except OSError: pass
        if reader:
            reader.close()
        elif p is not None:
            for stream in (p.stdout,p.stderr):
                try:
                    if stream: stream.close()
                except OSError: pass
        if log:
            try: log.close()
            except OSError: pass
        if sigterm_installed:
            signal.signal(signal.SIGTERM, previous_sigterm)
