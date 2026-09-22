"""Bounded JSONL consumption; raw logs are opt-in and never returned inline."""
import json
import submission
import hashlib
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time

MAX_LINE = 4 * 1024 * 1024
PROGRESS_HEARTBEAT_SECONDS = 60

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
            self.bad_line(); return
        try:
            self.safe = self.safe and self.replay_check([event])
            kind = event.get('type')
            self.prework = self.prework and kind in ('step_start', 'error')
            part = event.get('part', {})
            sid = event.get('sessionID')
            if isinstance(sid, str):
                if self.session and self.session != sid:
                    self.bad_line(); return
                self.session = sid[:200]
            if kind == 'error':
                error = event.get('error', {})
                data = error.get('data', {})
                if len(self.errors) >= 32:
                    self.bad_line(); return
                code=data.get('statusCode')
                if code is not None and (type(code) is not int or not 100<=code<=599):
                    self.bad_line(); code=None
                self.errors.append({'type':'error', 'error': {
                    'name':str(error.get('name',''))[:100],
                    'data':{'statusCode':code, 'message':str(data.get('message',''))[:1000]}}})
                self._set_progress_phase('provider_error')
            elif kind == 'step_finish':
                self.record_usage(event)
                reason=part.get('reason')
                if reason not in ('stop','length','content-filter','tool-calls','error','other','unknown'):
                    self.bad_line();reason='unknown'
                self.last_finish = {'type':'step_finish','part':{'reason':reason}}
            elif kind == 'text':
                text = str(part.get('text',''))
                self.answer = text[:4000]
                self.truncated |= len(text) > 4000
            elif kind == 'tool_use':
                self.submission.consume(part, sid)
                name = str(part.get('tool','unknown'))[:100]
                self.activity_sequence += 1
                if name not in ('read', 'glob', 'grep', 'list', 'bash', submission.TOOL):
                    self.last_non_shell_activity = self.activity_sequence
                if name not in self.tools and len(self.tools) >= 64: name = 'other'
                call=part.get('callID') or part.get('id')
                key=hashlib.sha256((str(event.get('sessionID',''))+'|'+str(call)).encode()).hexdigest() if call else None
                if key is None or key not in self.tool_calls:
                    self.tools[name] = self.tools.get(name, 0) + 1
                if key and len(self.tool_calls)<10000: self.tool_calls.add(key)
                elif key: self.truncated=True
                state = part.get('state', {})
                self.denied |= 'rejected permission' in str(state.get('error','')).lower()
                tool_status=state.get('status')
                if tool_status not in ('completed','error','running','pending'):
                    self.bad_line(); tool_status='unknown'
                if name in ('read','glob','grep','list') and self.progress_phase=='starting':
                    self._set_progress_phase('exploring')
                elif name == submission.TOOL and tool_status=='completed':
                    self._set_progress_phase('finishing')
                elif name not in ('read','glob','grep','list','bash',submission.TOOL) and self.progress_phase=='starting':
                    self._set_progress_phase('exploring')
                # Bounded observed status per tool; counts remain the authoritative
                # "was it called" signal. Status is recorded only when available and
                # transitions once per call ID to avoid double counting updates.
                if tool_status in ('completed','error') and name in self.tools:
                    previous=self.tool_call_status.get(key) if key else None
                    if key is None or previous!=tool_status:
                        states=self.tool_status.setdefault(name, {'completed':0,'error':0})
                        states[tool_status]=states.get(tool_status,0)+1
                    if key and len(self.tool_call_status)<10000:
                        self.tool_call_status[key]=tool_status
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
                    # Terminal updates replace running/duplicate calls; a bounded
                    # recent-command tail stays available as observed writer evidence.
                    previous=next((x for x in self.commands if x['_call']==identity), None)
                    if previous:
                        if any(previous[k]!=record[k] for k in ('command','command_truncated','tool_status','exit')):
                            self.bad_line()
                        # Duplicate terminal events are not a fresh test rerun.
                        record['sequence']=previous['sequence']
                    self.commands=[x for x in self.commands if x['_call']!=identity]
                    self.commands.append(record)
                    self.commands=self.commands[-10:]
                    if _looks_like_validation(command):
                        if record['exit'] not in (None,0) or tool_status=='error':
                            self.validation_failed=True
                            self._set_progress_phase('validation_failed')
                        else:
                            self.validation_failed=False
                            self._set_progress_phase('validating')
                    elif self.progress_phase=='starting':
                        self._set_progress_phase('exploring')

        except (TypeError, AttributeError, ValueError):
            self.bad_line()

    def classification_events(self):
        result=list(self.errors)
        if self.last_finish: result.append(self.last_finish)
        if self.denied: result.append({'type':'tool_use','part':{'state':{'error':'rejected permission'}}})
        return result

    def public(self):
        return {'result_submission':self.submission.public(), 'session_id':self.session, 'tools':self.tools, 'tool_status':self.tool_status, 'worker_tokens':self.token_usage(),
                'last_non_shell_activity':self.last_non_shell_activity,
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
        # A temporary stdin file prevents pipe-write deadlock on large task prompts.
        with tempfile.TemporaryFile() as stdin:
            stdin.write(prompt.encode()); stdin.seek(0)
            p=subprocess.Popen(['opencode',*args],cwd=cwd,env=env,stdin=stdin,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
            launched=True
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
                    progress_stream.write(' · '.join(parts)+'\n'); progress_stream.flush()
                except (OSError,ValueError):
                    pass
                last_progress_at=now; last_progress_revision=summary.progress_revision
            show_progress(force=True)
            selector=selectors.DefaultSelector()
            selector.register(p.stdout,selectors.EVENT_READ,'stdout')
            selector.register(p.stderr,selectors.EVENT_READ,'stderr')
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
            try:
                while selector.get_map():
                    kind=deadline_reached()
                    if kind:
                        error=TimeoutError('Worker inactivity timeout' if kind=='inactivity' else 'Worker timeout')
                        error.timeout_kind=kind
                        raise error
                    for key,_ in selector.select(select_window()):
                        chunk=os.read(key.fileobj.fileno(),65536)
                        if not chunk:
                            selector.unregister(key.fileobj); continue
                        if key.data=='stderr':
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
                                        summary.consume(json.loads(buffer)); mark_activity(); show_progress()
                                    except (ValueError,UnicodeError): summary.bad_line()
                                buffer=b''; discard=False
                    show_progress()
                if buffer.strip() and not discard:
                    try:
                        summary.consume(json.loads(buffer)); mark_activity(); show_progress()
                    except (ValueError,UnicodeError): summary.bad_line()
                try:
                    rc=p.wait(timeout=wait_timeout())
                except subprocess.TimeoutExpired as error:
                    error.timeout_kind=deadline_reached() or 'inactivity'
                    raise
                completed=True
                summary._set_progress_phase('process_exited')
                show_progress(force=True)
                return rc,summary,stderr_present,log_path
            finally:
                selector.close()
    except (TimeoutError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        raise attach(error)
    except OSError as error:
        # Launched-process I/O failures after execution started must not be
        # relabelled as launch_failed; they carry observed session evidence.
        raise attach(error)
    finally:
        if p is not None:
            if not completed:
                try: os.killpg(p.pid,signal.SIGTERM)
                except (ProcessLookupError, OSError): pass
                # A reaped leader does not imply its children stopped. Kill the
                # remaining group after grace even if the leader already exited.
                grace=time.monotonic()+0.5
                while time.monotonic()<grace:
                    p.poll()
                    try: os.killpg(p.pid,0)
                    except (ProcessLookupError, OSError): break
                    time.sleep(0.02)
                try: os.killpg(p.pid,signal.SIGKILL)
                except (ProcessLookupError, OSError): pass
                try: p.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError): pass
            for stream in (p.stdout, p.stderr):
                try:
                    if stream: stream.close()
                except OSError: pass
        if log:
            try: log.close()
            except OSError: pass
