#!/usr/bin/env python3
"""Sequential, provider-neutral OpenCode Ultra Lite control plane. Python standard library only."""
import argparse
import copy
import streaming
import submission
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

BASE = {'default': None, 'writer_default': None, 'fallbacks': [], 'aliases': {}, 'projects': {}, 'variants': {}, 'mode': 'auto', 'project_modes': {}}
CONFIG = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home()/'.config')))/'opencode-worker/config.json'

class Failure(Exception):
    def __init__(self, code, message, extra=None):
        self.code, self.message = code, message
        # Bounded structured detail; never raw model content.
        self.extra = extra if isinstance(extra, dict) else {}
        super().__init__(message)

def encode_result(value,compact=False):
    def dumps(obj):
        return json.dumps(obj,ensure_ascii=False,separators=(',',':')) if compact else json.dumps(obj,ensure_ascii=False,indent=2)
    if 'worker_used' in value:
        value['returned_bytes']=0
        while True:
            payload=dumps(value)+'\n'
            size=len(payload.encode('utf-8'))
            if value['returned_bytes']==size: return payload
            value['returned_bytes']=size
    return dumps(value)+'\n'

def emit(value,compact=False):
    sys.stdout.write(encode_result(value,compact=compact))

COMPACT_SUMMARY_CHARS=600
COMPACT_CHANGED_FILE_SAMPLE=10
COMPACT_PATH_CHARS=120

def _compact_attempt(attempt):
    out={k:attempt[k] for k in ('role','route','variant','status','session_id','elapsed_seconds','summary_truncated') if attempt.get(k) is not None}
    if attempt.get('tools'): out['tools']=attempt['tools']
    if attempt.get('worker_tokens'): out['worker_tokens']=attempt['worker_tokens']
    if attempt.get('provider_errors'): out['provider_errors']=attempt['provider_errors']
    answer=attempt.get('worker_answer')
    if answer: out['summary']=answer[:COMPACT_SUMMARY_CHARS]
    return out

def compact_output(result):
    """Bounded Codex-facing view: no raw command catalogs, reasoning, diffs or file bodies.
    Detailed attempts, shell metadata and private captures remain in private evidence files."""
    if result.get('ultra_lite'):
        return result['public_result']
    keep=('status','worker_used','worker_process_started','token_coverage','worker_evidence_note',
          'route_source','evidence_dir','error_code','message','mode','config','next','note')
    out={k:result[k] for k in keep if result.get(k) is not None}
    out['attempts']=[_compact_attempt(a) for a in result.get('attempts',[])]
    if result.get('worker_tokens_all_attempts'): out['worker_tokens_all_attempts']=result['worker_tokens_all_attempts']
    # Bounded path sample plus an explicit omission count; no unbounded path array.
    changed=[str(n) for n in result.get('changed_files') or []]
    if changed:
        out['changed_file_count']=len(changed)
        out['changed_files']=[n[:COMPACT_PATH_CHARS] for n in changed[:COMPACT_CHANGED_FILE_SAMPLE]]
        if len(changed)>COMPACT_CHANGED_FILE_SAMPLE:
            out['changed_files_omitted']=len(changed)-COMPACT_CHANGED_FILE_SAMPLE
    return out

def execution_evidence(result):
    attempts=result.get('attempts',[])
    result['worker_used']=any(a.get('session_id') for a in attempts)
    result['worker_process_started']=any(a.get('status')!='launch_failed' for a in attempts)
    fields=('input','output','reasoning','cache_read','cache_write','reported_total')
    result['worker_tokens_all_attempts']={k:None for k in fields}
    for field in fields:
        values=[a.get('worker_tokens',{}).get('reported',{}).get(field) for a in attempts]
        known=[v for v in values if type(v) is int]
        if known: result['worker_tokens_all_attempts'][field]=sum(known)
    result['token_coverage']='observed_step_events' if attempts and all(a.get('worker_tokens',{}).get('coverage')=='observed_step_events' for a in attempts) else 'partial_or_unavailable'
    if not result['worker_used']:
        result['worker_evidence_note']='No session observed; process launch alone does not prove a model request.'
    return result

def invoke(args, cwd=None, timeout=60, prompt=None, env=None):
    try:
        p = subprocess.Popen(['opencode', *args], cwd=cwd, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError as e:
        raise Failure('launch_failed', str(e))
    try:
        out, err = p.communicate(prompt, timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        os.killpg(p.pid, signal.SIGTERM)
        try:
            p.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
        raise Failure('interrupted', 'Worker stopped; inspect repository before retry. No automatic fallback.')
    return p.returncode, out, err

def discovery_failure(returncode, stderr):
    """Expose only fixed diagnostic categories, never CLI text, paths or credentials."""
    diagnostic = stderr[:8192].lower()
    if 'filesystem.open' in diagnostic and re.search(r'opencode[/\\]log[/\\]', diagnostic):
        cause = 'OpenCode log file could not be opened; check host filesystem permissions.'
    elif re.search(r'permission denied|operation not permitted|\beacces\b|\beperm\b', diagnostic):
        cause = 'OpenCode filesystem access was denied; check host filesystem permissions.'
    elif re.search(r'\benotfound\b|\beconnrefused\b|\betimedout\b|fetch failed', diagnostic):
        cause = 'OpenCode reported a network connection failure.'
    elif re.search(r'providerautherror|authentication failed|unauthorized|invalid api key', diagnostic):
        cause = 'OpenCode reported an authentication failure.'
    else:
        cause = 'OpenCode model discovery failed; no recognized safe diagnostic. Inspect OpenCode locally.'
    return Failure('discovery_failed', f'{cause} (exit {returncode})')


def models(cwd):
    rc, out, err = invoke(['models'], cwd)
    if rc:
        raise discovery_failure(rc, err)
    return sorted(set(x.strip() for x in out.splitlines() if re.fullmatch(r'[^\s/]+/[^\s]+', x.strip())))

def load(path):
    d = json.loads(path.read_text()) if path.exists() else copy.deepcopy(BASE)
    if not isinstance(d, dict):
        raise Failure('invalid_config', 'Configuration must be an object.')
    d = {**copy.deepcopy(BASE), **d}
    if not (d['default'] is None or isinstance(d['default'], str)) or not isinstance(d['fallbacks'], list) or not all(isinstance(x,str) for x in d['fallbacks']):
        raise Failure('invalid_config', 'Invalid default or fallbacks.')
    if d['writer_default'] is not None and not isinstance(d['writer_default'],str):
        raise Failure('invalid_config','Invalid writer_default')
    for key in ('aliases','projects','variants','project_modes'):
        if not isinstance(d[key],dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in d[key].items()):
            raise Failure('invalid_config', 'Invalid '+key)
    if d['mode'] not in ('auto','manual','off') or any(v not in ('auto','manual','off') for v in d['project_modes'].values()):
        raise Failure('invalid_config','Mode must be auto, manual or off.')
    return d

@contextlib.contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure('busy', 'Another worker or config update is active.')
        yield

def execution_lock_path(root):
    """Share one lock per canonical checkout across workflow variants/config files."""
    canonical_root = project(root)
    key = hashlib.sha256(os.fsencode(canonical_root)).hexdigest()
    return CONFIG.parent / 'locks' / (key + '.lock')

@contextlib.contextmanager
def execution_lock(root):
    try:
        with lock(execution_lock_path(root)):
            yield
    except Failure as error:
        if error.code == 'busy':
            raise Failure('busy', 'Another worker is active in this project directory.') from error
        raise

def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.config-')
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(data,f,ensure_ascii=False,indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)

def project(path):
    p = Path(path).resolve()
    if not p.is_dir(): raise Failure('invalid_project','Project directory does not exist.')
    r = subprocess.run(['git','-C',str(p),'rev-parse','--show-toplevel'],capture_output=True,text=True)
    return str(Path(r.stdout.strip()).resolve()) if r.returncode==0 else str(p)

def canonical(route,d,available):
    route = d['aliases'].get(route,route)
    if route not in available:
        raise Failure('unknown_model', 'Not in current OpenCode model inventory: '+str(route))
    return route

def route_id(route,d):
    route=d['aliases'].get(route,route)
    if not isinstance(route,str) or not re.fullmatch(r'[^\s/]+/[^\s]+',route):
        raise Failure('invalid_route','Expected provider/model or saved alias.')
    return route

def validate_variant(route,variant,root):
    if variant is None: return
    rc,out,err=invoke(['models',route.split('/')[0],'--verbose'],root)
    if rc: raise discovery_failure(rc, err)
    # Verbose CLI emits an exact route line followed by a JSON object.
    lines=out.splitlines(keepends=True)
    for i,line in enumerate(lines):
        if line.strip()==route:
            data,_=json.JSONDecoder().raw_decode(''.join(lines[i+1:]).lstrip())
            if variant in data.get('variants',{}) and not data['variants'][variant].get('disabled',False): return
            raise Failure('unknown_variant','Unsupported variant for '+route+': '+variant)
    raise Failure('unknown_model','Model not found: '+route)

# Shared original skill roots are discovered generically; nothing is copied into this skill.
SHARED_SKILL_ROOTS=('.agents/skills','.codex/skills')
MANIFEST_NAMES=('.agents/worker-capabilities.json','.opencode-worker.json')
# Exact harmless informational invocations permitted after the opencode deny rules.
# Appended last so OpenCode last-match semantics allow only these forms, never run/serve/config.
INFORMATIONAL_BASH_ALLOW=('opencode --help','opencode -h','opencode --version','opencode -v')

def skill_catalog(directory,limit=200):
    items=[]
    directory=Path(directory)
    if not directory.is_dir(): return items
    try:
        for entry in sorted(directory.iterdir()):
            try:
                if (entry/'SKILL.md').is_file():
                    # Keep both the catalog entry and its resolved original so callers
                    # can grant native access to a symlink target outside the parent root.
                    items.append({'name':entry.name,'path':str(entry.resolve()),'resolved':str(entry.resolve())})
            except OSError: continue
    except OSError: return items
    return items[:limit]

def discover_context(root,home=None):
    """Generic, non-project-specific discovery: repo AGENTS.md, project skills, shared skill roots
    and an optional per-project capability manifest. Returns compact paths/catalog only; no content
    is copied and skills are read lazily by the worker."""
    project=Path(root); home=Path(home) if home else Path.home()
    instructions=[]
    for candidate in ('AGENTS.md','.agents/AGENTS.md'):
        p=project/candidate
        if p.is_file(): instructions.append({'name':candidate,'path':str(p.resolve())})
    project_skills=skill_catalog(project/'.agents/skills')
    shared=[]
    for rel in SHARED_SKILL_ROOTS:
        base=home/rel
        if base.is_dir():
            for item in skill_catalog(base):
                item['root']=str(base.resolve())
                shared.append(item)
    manifest=None;capabilities=[];extra_roots=[]
    for name in MANIFEST_NAMES:
        p=project/name
        if not p.is_file(): continue
        try:
            data=json.loads(p.read_text())
            if not isinstance(data,dict): raise ValueError('manifest must be an object')
            capabilities=[x for x in data.get('capabilities',[]) if isinstance(x,str)][:32]
            for value in data.get('shared_skills',[]):
                if not isinstance(value,str): continue
                match=next((s for s in shared if s['name']==value),None)
                if match: extra_roots.append(match['root'])
                else:
                    candidate=Path(value).expanduser()
                    if candidate.is_dir(): extra_roots.append(str(candidate.resolve()))
            manifest={'path':str(p.resolve()),'capabilities':capabilities,'shared_skills':[x for x in data.get('shared_skills',[]) if isinstance(x,str)][:32]}
        except (OSError,ValueError) as e:
            manifest={'path':str(p.resolve()),'error':'invalid_manifest:'+str(e)[:120]}
        break
    roots=[str((home/rel).resolve()) for rel in SHARED_SKILL_ROOTS if (home/rel).is_dir()]
    roots=list(dict.fromkeys(roots+extra_roots))
    # Symlinked shared skills resolve to a real origin outside the parent catalog
    # (e.g. ~/.agents/skills => symlink to another directory). Grant native
    # read-only access to those resolved originals as well.
    discovered_roots=[s['resolved'] for s in shared+project_skills if s in shared or not Path(s['resolved']).is_relative_to(project.resolve())]
    # Symlinked shared skills may resolve to an original outside every catalog
    # root; those originals must themselves be granted read-only access. Add only
    # resolved paths not already covered by an existing root so real in-catalog
    # skills do not inflate the allowed root list.
    for resolved in discovered_roots:
        if any(resolved==r or resolved.startswith(r+os.sep) for r in roots): continue
        roots.append(resolved)
    roots=list(dict.fromkeys(roots))
    return {'instructions':instructions,'project_skills':project_skills,'shared_skills':shared,
            'manifest':manifest,'capabilities':capabilities,'shared_roots':roots,
            'discovered_roots':discovered_roots}

def context_prompt(ctx):
    lines=[]
    if ctx['instructions']:
        lines.append('Repository instruction files (read those relevant to the task): '+', '.join(i['path'] for i in ctx['instructions']))
    if ctx['project_skills']:
        lines.append('Project skill catalog (lazy read; paths): '+', '.join(s['path']+'/SKILL.md' for s in ctx['project_skills']))
    if ctx['shared_skills']:
        lines.append('Shared read-only skill catalog (lazy read only when relevant; original sources, never edit): '+', '.join(s['path']+'/SKILL.md' for s in ctx['shared_skills']))
    if ctx['capabilities']:
        lines.append('Project-declared capabilities: '+', '.join(ctx['capabilities']))
    return '\n'.join(lines)

def worker_env(read_only=False,commands=(),shared_roots=()):
    env=os.environ.copy()
    extra=json.loads(env.get('OPENCODE_CONFIG_CONTENT','{}'))
    if not isinstance(extra,dict) or ('agent' in extra and not isinstance(extra['agent'],dict)):
        raise Failure('invalid_environment','OPENCODE_CONFIG_CONTENT and its agent field must be objects.')
    profile=json.loads((Path(__file__).parent.parent/'assets/worker-agent.json').read_text())
    permission=profile['permission']
    if shared_roots:
        permission['external_directory']={'*':'deny'}
        for root in shared_roots:
            # Skill references/helpers may be read/executed, but never edited by a worker.
            permission['external_directory'][root]='allow'
            permission['external_directory'][root+'/*']='allow'
            permission['read'][root+'/*']='allow'
            permission['edit'][root]='deny'
            permission['edit'][root+'/*']='deny'
        # Shared-root allows are appended after the credential denies; re-assert the
        # credential/secret denies last (pop+re-add) so no symlinked original can
        # expose .env files. Last-match semantics then favor the deny.
        for table in ('read','edit'):
            secrets={k:v for k,v in permission[table].items() if k.endswith('.env') or '.env.' in k}
            for pattern in secrets: permission[table].pop(pattern,None)
            permission[table].update(secrets)
    if read_only:
        # Read-only discovery: deny every mutating capability. Native reads only;
        # project MCP/custom tools stay denied unless they are pure read/glob/grep/list.
        permission['edit']='deny'; permission['bash']='deny'
        permission['write']='deny'; permission['patch']='deny'; permission['task']='deny'
        for name in list(permission):
            if name in ('read','glob','grep','list','external_directory','doom_loop','*'): continue
            permission[name]='deny'
    else:
        # Writer: existing project MCP/custom tools remain usable through the normal
        # OpenCode interface; only explicit critical operations below stay denied.
        permission['*']='allow'
        # Keep deny rules last, including when an exact extra command overlaps one.
        denied={k:v for k,v in permission['bash'].items() if v=='deny'}
        permission['bash']={k:v for k,v in permission['bash'].items() if v!='deny'}
        for command in commands:
            if any(c in command for c in '*?\n\r'):
                raise Failure('invalid_command','Use exact approved commands without wildcards or newlines.')
            permission['bash'][command]='allow'
        for pattern,action in denied.items():
            permission['bash'].pop(pattern,None)
            permission['bash'][pattern]=action
        # Harmless tool discovery only: exact informational flags. These are appended
        # last so they override the broad opencode deny without permitting run/serve,
        # config or credential subcommands.
        for command in INFORMATIONAL_BASH_ALLOW:
            permission['bash'][command]='allow'
    extra.setdefault('agent',{})['codex-worker']=profile
    env['OPENCODE_CONFIG_CONTENT']=json.dumps(extra)
    return env

def safe_to_replay(events,malformed=False):
    if malformed: return False
    safe_events={'step_start','step_finish','text','reasoning','error'}
    for e in events:
        if e.get('type')=='tool_use':
            part=e.get('part',{}); state=part.get('state',{})
            if part.get('tool') not in ('read','glob','grep','list') or state.get('status') not in ('completed','error'):
                return False
        elif e.get('type') not in safe_events:
            return False
    return True

def resolve(d,root,override,inherit=False):
    for source,route in [('one-shot',override),('project',d['projects'].get(root)),('global',d.get('writer_default') or d['default'])]:
        if route: return source,route
    if inherit:
        rc,out,_=invoke(['debug','config'],root)
        if rc: raise Failure('config_probe_failed','Cannot resolve OpenCode configuration.')
        route=json.loads(out).get('model')
        if route: return 'opencode-config',route
    return 'setup',None

def classify(events,rc):
    # Only harness error events can trigger fallback, never assistant text or tool output.
    errors=[e.get('error',{}) for e in events if e.get('type')=='error']
    recognized = 0
    for e in errors:
        data=e.get('data',{}) if isinstance(e,dict) else {}
        name=e.get('name','') if isinstance(e,dict) else ''
        status=data.get('statusCode')
        message=str(data.get('message','')).lower()
        if name in ('ProviderAuthError','ModelNotFoundError','ProviderModelNotFoundError') or status in (401,403,404,429,502,503,504):
            recognized += 1
            continue
        if name in ('APIError','UnknownError') and re.search(r'quota|rate.limit|insufficient.credit|connection refused|econnreset|enotfound|fetch failed|model.+not found|provider.+unavailable',message):
            recognized += 1
            continue
    if errors:
        return 'provider_failure' if recognized == len(errors) else 'execution_failed'
    if rc: return 'execution_failed'
    if any(e.get('type')=='tool_use' and 'rejected permission' in str(e.get('part',{}).get('state',{}).get('error','')).lower() for e in events):
        return 'permission_denied'
    finishes=[e for e in events if e.get('type')=='step_finish']
    if not finishes or finishes[-1].get('part',{}).get('reason')!='stop': return 'incomplete'
    return 'completed_needs_review'

def run_worker(a,d,root,available=None):
    mode=d.get('project_modes',{}).get(root,d.get('mode','auto'))
    if mode=='off' or (mode=='manual' and not getattr(a,'explicit',False)):
        return {'status':'mode_blocked','mode':mode,'message':'Enable worker mode or explicitly invoke it in manual mode.'}
    source,route=resolve(d,root,a.model,a.use_opencode_default)
    if not route: return {'status':'setup_required','config':str(a.config),'next':'Run models to choose a route.'}
    route=route_id(route,d)
    if getattr(a,'variant',None): validate_variant(route,a.variant,root)
    routes=[route]
    if not a.no_fallback and (source!='one-shot' or getattr(a,'use_fallbacks',False)):
        routes += [route_id(r,d) for r in d['fallbacks']]
    routes=list(dict.fromkeys(routes))
    prompt=Path(a.task_file).read_text() if a.task_file else sys.stdin.read()
    if not prompt.strip(): raise Failure('empty_task','Supply --task-file or nonempty stdin.')
    prompt = 'Repository root: '+root+'\n'+prompt
    context=discover_context(root)
    context_lines=context_prompt(context)
    if context_lines:
        prompt+='\nDiscovered project context (paths only; read lazily and only what is relevant):\n'+context_lines
        prompt+='\nShared skill sources are read-only originals: follow their SKILL.md when the task needs them; never copy or edit them. Do not print credentials or descriptor/auth tokens.'
    prompt+='\nFinal verification: run each relevant test suite as its own direct, uncombined command (no shell chaining/pipes) and report the actual command and observed pass/fail counts. Compound-command exit 0 is not accepted as test evidence.'
    if getattr(a, '_submit_result', False):
        prompt+='\nAfter completing implementation and actual validation, call codex_worker_submit_result with status, changed, validation and risk. Use completed only when all requirements and validation are done; otherwise needs_escalation with the blocker. Keep the report within 1800 characters, with actual commands and pass/fail counts. If the tool rejects the report, correct ONLY the report and resubmit in this same session. Submit after your last development tool call. After acceptance finish; optional prose or Markdown is not machine protocol. Never fabricate validation. For remote tools verify actual completed results, not just transport exit.'
    else:
        prompt+='\nResolve normal failures yourself in this session. Final reply ONLY a small JSON object (no markdown): {"status":"completed" or "needs_escalation","changed":["relative paths"],"validation":["actual command: actual completed pass/fail counts"],"risk":"none or unresolved risk"}. Maximum 1800 characters. Completed means ALL requirements and validation are done; otherwise needs_escalation and explain the blocker in risk. No implementation narrative, logs, source, diff, or reasoning. For Unity/remote tools verify actual completed results, not just transport exit.'
    access_roots=list(dict.fromkeys(context['shared_roots']+context.get('discovered_roots',[])))
    env=worker_env(a.read_only,getattr(a,'allow_command',[]),access_roots)
    if getattr(a, '_submit_result', False):
        try: submission.configure(env)
        except ValueError as error: raise Failure('submission_setup_failed', str(error))
    attempts=[]
    # Checkout lock is shared across workflow variants and alternate config files.
    with (contextlib.nullcontext() if getattr(a,'_lock_held',False) else execution_lock(root)):
        for r in routes:
            variant=(getattr(a,'variant',None) if r==route else None) or d.get('variants',{}).get(r)
            args=['run','--format','json','--agent','codex-worker','--dir',root,'-m',r]
            if variant: args += ['--variant',variant]
            try:
                rc,summary,stderr_present,log_path=streaming.run(args,root,a.timeout,prompt,env,safe_to_replay,getattr(a,'log_dir',None))
            except (TimeoutError,subprocess.TimeoutExpired,KeyboardInterrupt) as error:
                status='interrupted'
                partial=getattr(error,'worker_summary',None)
                attempts.append({'route':r,'variant':variant,'status':status,'returncode':None,
                    **(partial.public() if partial else {}),'raw_log':getattr(error,'worker_log',None)})
                break
            except OSError as e:
                partial=getattr(e,'worker_summary',None)
                launched=getattr(e,'worker_launched',False)
                # Only a process that never started is a launch failure; once
                # execution began any I/O error keeps observed session evidence.
                status='interrupted' if launched else 'launch_failed'
                attempts.append({'route':r,'variant':variant,'status':status,'returncode':None,
                    **(partial.public() if partial else {'session_id':None}),'raw_log':getattr(e,'worker_log',None)})
                break
            events=summary.classification_events()
            status='incomplete' if summary.invalid else classify(events,rc)
            replay_safe=summary.safe
            errors=[e.get('error',{}) for e in summary.errors]
            masked_lookup=(errors and all(e.get('name')=='UnknownError' and
                e.get('data',{}).get('message')=='Unexpected server error. Check server logs for details.' for e in errors))
            if status=='execution_failed' and replay_safe and summary.prework and masked_lookup:
                try:
                    if r not in models(root): status='provider_failure'
                except Failure: pass
            attempts.append({'route':r,'variant':variant,'status':status,'returncode':rc,
                **summary.public(),'stderr_present':stderr_present,'raw_log':log_path})
            if status!='provider_failure' or not replay_safe:
                break  # Mutating, unknown or incomplete tool activity must be reviewed before replay.
    result={'status':status,'route_source':source,'attempts':attempts,
            'note':'CLI completion does not establish implementation/test success.'}
    return result

def run_head_fix(a,d,root):
    """One explicit Head-confirmed defect correction; never an automatic chain."""
    prior=Path(a.fix_from).expanduser().resolve()
    if a.read_only:
        raise Failure('needs_escalation','A corrective writer cannot be read-only.')
    with execution_lock(root):
        original=json.loads(prior.read_text())
        if not isinstance(original,dict) or not original.get('ultra_lite') or original.get('project_root')!=root:
            raise Failure('needs_escalation','Correction needs original Ultra Lite evidence for this project.')
        if original.get('correction_of'):
            raise Failure('needs_escalation','The one corrective writer has already been used; stop for further direction.')
        last=(original.get('attempts') or [{}])[-1]
        captured=last.get('result_submission') or {}
        if last.get('status')!='completed_needs_review' or captured.get('error'):
            raise Failure('needs_escalation','Transport/execution failure is not a confirmed implementation defect.')
        try: submission.validate(captured.get('report'))
        except ValueError as error:
            raise Failure('needs_escalation','Correction requires valid original result submission.') from error
        # Claim before launch; an interrupted/failed correction must not silently replay.
        try: fd=os.open(prior.parent/'head-fix-used',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError:
            raise Failure('needs_escalation','The one corrective writer has already been used; stop for further direction.')
        with os.fdopen(fd,'w') as f: f.write(str(prior)+'\n')
        args=copy.copy(a);args._lock_held=True;args._correction_of=str(prior)
        return run_direct(args,d,root)


def run_direct(a,d,root):
    """Ultra Lite writer runtime: one Writer plus optional concrete-defect fix, compact evidence."""
    parent=Path(a.evidence_dir or (CONFIG.parent/'reviews')).expanduser().resolve()
    if parent==Path(root) or Path(root) in parent.parents:
        raise Failure('invalid_evidence_dir','Use evidence outside the project.')
    parent.mkdir(parents=True,exist_ok=True)
    evidence=Path(tempfile.mkdtemp(prefix='lite-',dir=parent))
    started=time.monotonic()
    previous=getattr(a, '_submit_result', False)
    a._submit_result=True
    try: result=execution_evidence(run_worker(a,d,root))
    finally: a._submit_result=previous
    result['elapsed_seconds']=round(time.monotonic()-started,3)
    result['evidence_dir']=str(evidence)
    result['ultra_lite']=True
    result['project_root']=root
    if getattr(a,'_correction_of',None):result['correction_of']=a._correction_of
    last=(result.get('attempts') or [{}])[-1]
    captured=last.get('result_submission') or {}
    submission_error=None
    try:
        if captured.get('error'): raise ValueError(captured['error'])
        report=submission.validate(captured.get('report'))
    except ValueError as error:
        submission_error=str(error)
        report={'status':'needs_escalation','changed':[], 'validation':[], 'risk':'No valid final tool submission; inspect private evidence.'}
    execution_status=result['status']
    if execution_status!='completed_needs_review':
        report['status']='needs_escalation'
        report['failure_kind']='worker_execution_failed'
        report['execution_status']=execution_status
        if submission_error:report['submission_error']=submission_error
    elif submission_error:
        report['failure_kind']='result_submission_failed'
        report['submission_error']=submission_error
    result['status']=report['status']
    public={**report,'worker_used':result['worker_used'],'worker':{
        'model':last.get('route'),'variant':last.get('variant'),'session':last.get('session_id'),
        'elapsed_seconds':result['elapsed_seconds'],'reported_tokens':result['worker_tokens_all_attempts'].get('reported_total'),
        'token_coverage':result['token_coverage'],'tools':last.get('tools',{}),'attempts':len(result.get('attempts',[]))}}
    public['evidence_dir']=str(evidence)
    if getattr(a,'_correction_of',None):public['correction_used']=True
    result['public_result']=public
    path=evidence/'writer.json'
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:json.dump(result,f,ensure_ascii=False)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,default=CONFIG)
    p.add_argument('--project',default=os.getcwd())
    s=p.add_subparsers(dest='cmd',required=True)
    s.add_parser('status'); s.add_parser('models'); s.add_parser('context')
    q=s.add_parser('set-mode'); q.add_argument('mode',choices=['auto','manual','off'])
    q=s.add_parser('set-project-mode'); q.add_argument('mode',choices=['auto','manual','off','inherit'])
    r=s.add_parser('resolve'); r.add_argument('--model'); r.add_argument('--use-opencode-default',action='store_true')
    for cmd in ('set-default','set-writer-default','set-project'):
        q=s.add_parser(cmd); q.add_argument('route',help='Exact model ID, alias, or null to clear'); q.add_argument('--variant',help='Save variant for this exact model route')
    q=s.add_parser('set-fallbacks'); q.add_argument('routes',nargs='*')
    q=s.add_parser('set-alias'); q.add_argument('name'); q.add_argument('route')
    q=s.add_parser('run'); q.add_argument('--model'); q.add_argument('--task-file','--brief',dest='task_file'); q.add_argument('--timeout',type=int,default=3600)
    q.add_argument('--explicit',action='store_true',help='User explicitly requested worker execution'); q.add_argument('--use-fallbacks',action='store_true'); q.add_argument('--log-dir',help='Opt-in private raw JSONL log directory'); q.add_argument('--variant'); q.add_argument('--allow-command',action='append',default=[],help='Exact task-authorized shell command, repeatable'); q.add_argument('--read-only',action='store_true'); q.add_argument('--no-fallback',action='store_true'); q.add_argument('--use-opencode-default',action='store_true')
    q.add_argument('--evidence-dir',help='Outside-repository evidence parent; defaults to worker config directory/reviews')
    q.add_argument('--fix-from',type=Path,help='Original Ultra Lite writer.json; one correction for a concrete Head-confirmed defect, using --brief')
    q.add_argument('--full-output',action='store_true',help='Detailed stdout including private attempt fields; default is bounded compact summary')
    a=p.parse_args(); a.config=a.config.expanduser().resolve(); root=project(a.project); d=load(a.config)
    if a.cmd=='status':
        source,route=resolve(d,root,None)
        emit({'config_path':str(a.config),'config':d,'project':root,'route_source':source,'route':route,'effective_mode':d['project_modes'].get(root,d['mode'])}); return
    if a.cmd=='models':
        av=models(root)
        emit({'models':av,'providers':sorted({r.split('/')[0] for r in av}),
              'deepseek_v4_1_flash':[r for r in av if re.search(r'deepseek.*v4[.-]1.*flash',r,re.I)],
              'note':'Inventory is not an authentication, billing, or coding-capability guarantee.'}); return
    if a.cmd=='resolve':
        source,route=resolve(d,root,a.model,a.use_opencode_default)
        emit({'source':source,'route':route_id(route,d) if route else None,'status':'resolved' if route else 'setup_required'}); return
    if a.cmd=='context':
        ctx=discover_context(root)
        emit({'project':root,**ctx,'prompt':context_prompt(ctx)}); return
    if a.cmd=='run':
        def deliver(result):
            # Default is compact: bounded summary and private evidence paths; details
            # stay in the evidence directory. --full-output exposes private attempt fields.
            emit(result if a.full_output else compact_output(result))
        mode=d.get('project_modes',{}).get(root,d.get('mode','auto'))
        if mode=='off' or (mode=='manual' and not a.explicit):
            deliver(execution_evidence(run_worker(a,d,root)));sys.exit(2)
        with tempfile.TemporaryDirectory(prefix='worker-task-') as temporary:
            brief=Path(a.task_file).read_text() if a.task_file else sys.stdin.read()
            if not brief.strip() or len(brief.encode('utf-8'))>8192:
                raise Failure('invalid_brief','Supply a nonempty brief of at most 8 KiB.')
            brief_path=Path(temporary)/'brief.txt';brief_path.write_text(brief)
            a.task_file=str(brief_path)
            if not resolve(d,root,a.model,a.use_opencode_default)[1]:
                deliver(execution_evidence(run_worker(a,d,root)));sys.exit(2)
            result=run_head_fix(a,d,root) if a.fix_from else run_direct(a,d,root)
        deliver(result)
        if result['status']!='completed': sys.exit(2)
        return
    # Read, resolve aliases, validate variant and write under one configuration lock.
    with lock(a.config.with_suffix('.lock')):
        d=load(a.config)
        if a.cmd=='set-mode': d['mode']=a.mode
        elif a.cmd=='set-project-mode':
            if a.mode=='inherit': d['project_modes'].pop(root,None)
            else: d['project_modes'][root]=a.mode
        else:
            clearing=(a.cmd in ('set-default','set-writer-default','set-project') and a.route=='null') or (a.cmd=='set-fallbacks' and not a.routes)
            av=[] if clearing else models(root)
            checked=None
            if getattr(a,'variant',None):
                if clearing: raise Failure('invalid_variant','Cannot set variant while clearing route.')
                checked=canonical(a.route,d,av)
                validate_variant(checked,a.variant,root)
            if a.cmd=='set-default':
                d['default']=None if a.route=='null' else canonical(a.route,d,av)
                d['writer_default']=None  # Legacy setter remains effective.
            elif a.cmd=='set-writer-default':
                d['writer_default']=None if a.route=='null' else canonical(a.route,d,av)
            elif a.cmd=='set-project':
                if a.route=='null': d['projects'].pop(root,None)
                else: d['projects'][root]=canonical(a.route,d,av)
            elif a.cmd=='set-fallbacks': d['fallbacks']=list(dict.fromkeys(canonical(r,d,av) for r in a.routes))
            elif a.cmd=='set-alias':
                if '/' in a.name: raise Failure('invalid_alias','Alias must not contain slash.')
                d['aliases'][a.name]=canonical(a.route,d,av)
            if checked: d['variants'][checked]=a.variant
        save(a.config,d)
    emit({'status':'saved','config_path':str(a.config),'config':d})

if __name__=='__main__':
    try: main()
    except (Failure,ValueError,OSError) as e:
        payload={'status':'error','code':getattr(e,'code','invalid_input'),'message':str(e)}
        extra=getattr(e,'extra',None) or {}
        missing=extra.get('missing_fields')
        if missing: payload['missing_fields']=missing
        emit(payload)
        sys.exit(2)
