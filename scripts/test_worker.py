import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('worker',Path(__file__).with_name('worker.py'))
w=importlib.util.module_from_spec(spec); spec.loader.exec_module(w)

def stream_result(rc,text,err=''):
    summary=w.streaming.Summary(w.safe_to_replay)
    for line in text.splitlines(): summary.consume(json.loads(line))
    return rc,summary,bool(err),None

def task_text(goal='Implement the requested behavior', **over):
    """A short free-form Ultra Lite brief; no structured contract is required."""
    brief={'goal':goal,
        'required_behavior':['Preserve literal `x` $(x) "quotes"','Include the second line'],
        'important_constraints':['Do not change unrelated behavior'],
        'already_decided_design':'Use the existing module layout',
        'do_not_change':['Public API shape'],
        'definition_of_done':['All relevant tests pass'],
        'validation':['Run python3 -m unittest discover'],
        'known_edge_cases':[]}
    brief.update(over)
    return json.dumps(brief)

class Tests(unittest.TestCase):
    def test_discovery_log_open_failure_is_safe_and_not_retried(self):
        # Actual restricted-host failure, with private path/credential-shaped noise.
        stderr = "\x1b[91mError: Unexpected error\x1b[0m\nUnknown: FileSystem.open (/Users/private/.local/share/opencode/log/opencode.log)\nAuthorization: Bearer PRIVATE_TOKEN"
        for call in (lambda: w.models('/tmp'),
                     lambda: w.validate_variant('provider/model', 'max', '/tmp')):
            with self.subTest(call=call), patch.object(w, 'invoke', return_value=(1, '', stderr)) as invoke:
                with self.assertRaises(w.Failure) as raised:
                    call()
                self.assertEqual(raised.exception.code, 'discovery_failed')
                self.assertIn('log file could not be opened', str(raised.exception))
                self.assertIn('exit 1', str(raised.exception))
                self.assertNotIn('/Users/', str(raised.exception))
                self.assertNotIn('PRIVATE_TOKEN', str(raised.exception))
                invoke.assert_called_once()
        # Unknown stderr is not echoed as a fallback.
        self.assertNotIn('PRIVATE_TOKEN', str(w.discovery_failure(1, 'PRIVATE_TOKEN')))

    def test_config_isolation_and_environment(self):
        with tempfile.TemporaryDirectory() as t:
            a=w.load(Path(t)/'missing');b=w.load(Path(t)/'missing')
            a['aliases']['new']='a/a'
            self.assertNotIn('new',b['aliases'])
        for value in ('null','[]','{"agent":null}'):
            with patch.dict(os.environ,{'OPENCODE_CONFIG_CONTENT':value}):
                with self.assertRaises(w.Failure): w.worker_env()

    def test_modes_and_oneshot(self):
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as t:
            task=Path(t)/'task';task.write_text('task')
            a=Namespace(model='a/a',use_opencode_default=False,no_fallback=False,task_file=str(task),read_only=True,timeout=5,config=Path(t)/'cfg')
            d={**w.BASE,'default':'a/a','fallbacks':['b/b'],'mode':'off'}
            with patch.object(w.streaming,'run') as run:
                self.assertEqual(w.run_worker(a,d,t)['status'],'mode_blocked');run.assert_not_called()
            d['mode']='manual'
            self.assertEqual(w.run_worker(a,d,t)['status'],'mode_blocked')
            a.explicit=True
            error=json.dumps({'type':'error','error':{'name':'ProviderAuthError'}})
            with patch.object(w,'CONFIG',Path(t)/'cfg'),patch.object(w.streaming,'run',side_effect=lambda *args:stream_result(1,error)) as run:
                self.assertEqual(len(w.run_worker(a,d,t)['attempts']),1)
                a.use_fallbacks=True
                self.assertEqual(len(w.run_worker(a,d,t)['attempts']),2)
            d['project_modes']={t:'off'}
            self.assertEqual(w.run_worker(a,d,t)['status'],'mode_blocked')

    def test_summary_excludes_source_and_is_bounded(self):
        summary=w.streaming.Summary(w.safe_to_replay)
        summary.consume({'type':'tool_use','part':{'tool':'read','state':{'status':'completed','output':'SECRET_SOURCE','metadata':{'display':{'text':'SECRET_SOURCE'}}}}})
        summary.consume({'type':'text','part':{'text':'x'*10000}})
        public=json.dumps(summary.public())
        self.assertNotIn('SECRET_SOURCE',public)
        self.assertLess(len(public),5000)
        self.assertTrue(summary.public()['summary_truncated'])
        summary.consume({'type':'tool_use','part':{'tool':'bash','state':{'status':'completed','input':{'command':'test'},'metadata':{'exit':3,'output':'SECRET_SOURCE'}}}})
        self.assertEqual(summary.commands[0]['exit'],3)
        self.assertFalse(summary.safe)

    def test_oversized_metadata(self):
        summary=w.streaming.Summary(w.safe_to_replay)
        summary.consume({'type':'error','error':{'data':{'statusCode':'x'*1000000}}})
        summary.consume({'type':'tool_use','part':{'tool':'bash','state':{'status':'x'*1000000}}})
        self.assertLess(len(json.dumps(summary.public())),2000)
        self.assertTrue(summary.invalid)

    def test_timeout_kills_descendants(self):
        import time
        with tempfile.TemporaryDirectory() as t:
            exe=Path(t)/'opencode';marker=Path(t)/'survived'
            exe.write_text('#!/usr/bin/env python3\nimport subprocess,sys,time\nsubprocess.Popen([sys.executable,"-c",'+repr('import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(1.5); pathlib.Path('+repr(str(marker))+').write_text("alive")')+'])\ntime.sleep(10)\n')
            exe.chmod(0o700)
            env={**os.environ,'PATH':t+os.pathsep+os.environ['PATH']}
            with self.assertRaises(TimeoutError): w.streaming.run(['run'],t,0.3,'task',env,w.safe_to_replay)
            time.sleep(1)
            self.assertFalse(marker.exists())

    def test_streaming_timeout_and_oversize(self):
        with tempfile.TemporaryDirectory() as t:
            exe=Path(t)/'opencode';exe.chmod(0o700) if exe.exists() else None
            env={**os.environ,'PATH':t+os.pathsep+os.environ['PATH']}
            exe.write_text('#!/usr/bin/env python3\nimport time\ntime.sleep(10)\n');exe.chmod(0o700)
            with self.assertRaises(TimeoutError):
                w.streaming.run(['run'],t,0.1,'task',env,w.safe_to_replay)
            exe.write_text('#!/usr/bin/env python3\nprint("x"*10000)\n')
            with patch.object(w.streaming,'MAX_LINE',100):
                _,summary,_,_=w.streaming.run(['run'],t,5,'task',env,w.safe_to_replay)
            self.assertTrue(summary.invalid);self.assertFalse(summary.safe)

    def test_streaming_large_stderr_and_raw_log(self):
        with tempfile.TemporaryDirectory() as t:
            exe=Path(t)/'opencode'
            exe.write_text('#!/usr/bin/env python3\nimport sys,json\nsys.stderr.write("e"*200000)\nprint(json.dumps({"type":"text","part":{"text":"done"}}))\nprint(json.dumps({"type":"step_finish","part":{"reason":"stop"}}))\n')
            exe.chmod(0o700)
            env={**os.environ,'PATH':t+os.pathsep+os.environ['PATH']}
            rc,summary,err,log=w.streaming.run(['run'],t,10,'task',env,w.safe_to_replay,t)
            self.assertEqual(rc,0);self.assertTrue(err);self.assertEqual(summary.answer,'done')
            self.assertEqual(Path(log).stat().st_mode & 0o777,0o600)
            self.assertIn('step_finish',Path(log).read_text())

    def test_usage_dedup_missing_and_bytes(self):
        summary=w.streaming.Summary(w.safe_to_replay)
        event={'type':'step_finish','sessionID':'s','part':{'id':'p','reason':'stop','tokens':{'input':10,'output':3,'reasoning':1,'total':13,'cache':{'read':5,'write':0}}}}
        summary.consume(event);summary.consume(event)
        usage=summary.token_usage()
        self.assertEqual(usage['reported']['input'],10)
        self.assertEqual(usage['reported']['reported_total'],13)
        self.assertEqual(usage['observed_steps'],1)
        evidence=w.execution_evidence({'status':'completed_needs_review','attempts':[{'status':'completed_needs_review',**summary.public()}]})
        payload=w.encode_result(evidence)
        self.assertEqual(json.loads(payload)['returned_bytes'],len(payload.encode()))
        self.assertNotIn('events',json.loads(payload)['attempts'][0])
        summary.consume({'type':'step_finish','part':{'id':'missing','reason':'stop'}})
        self.assertEqual(summary.token_usage()['coverage'],'partial_or_unavailable')
        absent=w.execution_evidence({'status':'mode_blocked'})
        self.assertFalse(absent['worker_used']);self.assertIsNone(absent['worker_tokens_all_attempts']['input'])

    def test_partial_timeout_evidence(self):
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as t:
            task=Path(t)/'task';task.write_text('task')
            a=Namespace(model='a/a',use_opencode_default=False,no_fallback=True,task_file=str(task),read_only=True,timeout=5,config=Path(t)/'cfg')
            summary=w.streaming.Summary(w.safe_to_replay)
            summary.consume({'type':'step_finish','sessionID':'s','part':{'id':'p','reason':'tool-calls','tokens':{'input':10,'output':2}}})
            error=TimeoutError();error.worker_summary=summary
            with patch.object(w,'CONFIG',Path(t)/'cfg'),patch.object(w.streaming,'run',side_effect=error):
                result=w.execution_evidence(w.run_worker(a,w.BASE,t))
            self.assertEqual(result['status'],'interrupted')
            self.assertTrue(result['worker_used']);self.assertEqual(result['worker_tokens_all_attempts']['input'],10)
            self.assertEqual(result['token_coverage'],'partial_or_unavailable')

    def test_launch_failure(self):
        with patch.object(w.subprocess,'Popen',side_effect=FileNotFoundError('opencode missing')):
            with self.assertRaises(w.Failure) as caught:
                w.invoke(['run'])
        self.assertEqual(caught.exception.code,'launch_failed')

    def test_priority(self):
        d={**w.BASE,'default':'a/a','projects':{'/repo':'b/b'}}
        self.assertEqual(w.resolve(d,'/repo','c/c'),('one-shot','c/c'))
        self.assertEqual(w.resolve(d,'/repo',None),('project','b/b'))
        self.assertEqual(w.resolve(d,'/other',None),('global','a/a'))
        self.assertEqual(w.resolve(w.BASE,'/repo',None),('setup',None))
    def test_errors(self):
        error={'type':'error','error':{'name':'APIError','data':{'statusCode':429}}}
        self.assertEqual(w.classify([error],0),'provider_failure')
        self.assertEqual(w.classify([{'type':'text','part':{'text':'quota exhausted'}},{'type':'step_finish','part':{'reason':'stop'}}],0),'completed_needs_review')
        self.assertEqual(w.classify([{'type':'tool_use','part':{'state':{'status':'error','error':'tests failed'}}},{'type':'step_finish','part':{'reason':'stop'}}],0),'completed_needs_review')
        self.assertEqual(w.classify([],1),'execution_failed')
        self.assertEqual(w.classify([],0),'incomplete')
        self.assertEqual(w.classify([{'type':'tool_use','part':{'state':{'error':'The user rejected permission to use this specific tool call.'}}}],0),'permission_denied')
    def test_mixed_errors_and_incomplete_steps(self):
        known={'type':'error','error':{'name':'APIError','data':{'statusCode':429}}}
        unknown={'type':'error','error':{'name':'PermissionError','data':{}}}
        self.assertEqual(w.classify([known,unknown],1),'execution_failed')
        self.assertEqual(w.classify([{'type':'step_finish','part':{'reason':'tool-calls'}}],0),'incomplete')

    def test_cli(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); exe=root/'opencode'; cfg=root/'config.json'; task=root/'task.txt'
            task.write_text(task_text())
            exe.write_text('''#!/usr/bin/env python3
import sys,json
if sys.argv[1]=='models':
 if '--verbose' in sys.argv:
  print('b/b'); print(json.dumps({'variants':{'max':{'reasoningEffort':'max'}}}))
 else: print('a/a\\nb/b')
elif sys.argv[1]=='run':
 prompt=sys.stdin.read()
 if '-m' not in sys.argv: sys.exit(8)
 if sys.argv[sys.argv.index('-m')+1] in ('a/a','removed/model'):
  print(json.dumps({'type':'error','error':{'name':'APIError','data':{'statusCode':429}}}))
 else:
  report={'status':'completed','changed':[],'validation':['second line: 2 passed'],'risk':'none'}
  print(json.dumps({'type':'tool_use','sessionID':'s','part':{'tool':'codex_worker_submit_result','callID':'submit','state':{'status':'completed','input':report,'output':json.dumps({'accepted':True,'report':report})}}}))
  print(json.dumps({'type':'text','part':{'text':json.dumps({'status':'completed','changed':[],'validation':['second line: 2 passed'],'risk':'none'})}}))
  print(json.dumps({'type':'step_finish','part':{'reason':'stop'}}))
'''); exe.chmod(0o700)
            env={**os.environ,'PATH':str(root)+os.pathsep+os.environ['PATH'],'XDG_CONFIG_HOME':str(root)}
            (root/'repo').mkdir()
            env['OPENCODE_CONFIG_DIR']=str(root/'sdk-config')
            sdk=root/'sdk-config/node_modules/@opencode-ai/plugin/dist/tool.js';sdk.parent.mkdir(parents=True);sdk.write_text('// fake CLI does not import SDK')
            base=['python3',str(Path(w.__file__).resolve()),'--config',str(cfg),'--project',str(root/'repo')]
            def call(*args):
                p=subprocess.run(base+list(args),capture_output=True,text=True,env=env)
                return p.returncode,json.loads(p.stdout)
            self.assertEqual(call('status')[1]['route_source'],'setup')
            self.assertEqual(call('run','--task-file',str(task))[1]['status'],'setup_required')
            self.assertEqual(call('set-default','a/a')[0],0)
            self.assertEqual(call('set-default','b/b')[1]['config']['default'],'b/b')
            self.assertEqual(call('resolve','--model','a/a')[1]['route'],'a/a')
            self.assertEqual(call('status')[1]['config']['default'],'b/b')
            self.assertEqual(call('set-default','missing/no')[0],2)
            call('set-default','a/a'); call('set-fallbacks','b/b')
            rc,result=call('run','--task-file',str(task))
            self.assertEqual(rc,0); self.assertEqual(result['worker']['attempts'],2)
            self.assertIn('second line',result['validation'][0])
            self.assertNotIn('shell_commands',result['worker'])
            # Full output remains available for compatibility.
            full=call('run','--task-file',str(task),'--full-output')[1]
            self.assertIn('second line',full['attempts'][1]['worker_answer'])
            self.assertEqual(call('set-default','b/b','--variant','max')[0],0)
            variant_result=call('run','--task-file',str(task))[1]
            self.assertEqual(variant_result['worker']['variant'],'max')
            stale=json.loads(cfg.read_text());stale['fallbacks']=['removed/model'];cfg.write_text(json.dumps(stale))
            self.assertEqual(call('run','--task-file',str(task))[0],0)
            call('set-project','b/b')
            self.assertEqual(call('resolve')[1]['source'],'project')
            cfgdata=json.loads(cfg.read_text());cfgdata['unrelated']={'keep':True};cfg.write_text(json.dumps(cfgdata))
            self.assertEqual(call('set-default','b/b')[1]['config']['unrelated'],{'keep':True})
    def test_replay_boundary(self):
        def tool(name,status): return {'type':'tool_use','part':{'tool':name,'state':{'status':status}}}
        self.assertTrue(w.safe_to_replay([tool('read','completed'),{'type':'text'}]))
        for name in ('edit','write','apply_patch','bash','task','custom_mcp'):
            for status in ('completed','error','running'):
                self.assertFalse(w.safe_to_replay([tool(name,status)]))
        self.assertFalse(w.safe_to_replay([tool('read','running')]))
        self.assertFalse(w.safe_to_replay([],True))

    def test_masked_missing_model(self):
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as t:
            task=Path(t)/'task';task.write_text('task')
            a=Namespace(model=None,use_opencode_default=False,no_fallback=False,task_file=str(task),read_only=True,timeout=5,config=Path(t)/'cfg')
            error={'type':'error','error':{'name':'UnknownError','data':{'message':'Unexpected server error. Check server logs for details.'}}}
            done={'type':'step_finish','part':{'reason':'stop'}}
            with patch.object(w,'CONFIG',Path(t)/'config.json'),patch.object(w,'models',return_value=['b/b']) as discovery,patch.object(w.streaming,'run',side_effect=[stream_result(1,json.dumps(error)),stream_result(0,json.dumps(done))]):
                result=w.run_worker(a,{**w.BASE,'default':'removed/model','fallbacks':['b/b']},t)
                self.assertEqual(len(result['attempts']),2)
                self.assertEqual(discovery.call_count,1)

    def test_development_command_policy(self):
        import re
        def decision(command, extras=()):
            rules=json.loads(w.worker_env(False,extras)['OPENCODE_CONFIG_CONTENT'])['agent']['codex-worker']['permission']['bash']
            result=None
            for pattern,action in rules.items():
                regex=re.escape(pattern).replace(r'\*','.*').replace(r'\?','.')
                if re.fullmatch(regex,command,re.S): result=action
            return result
        for command in ('ls -al','ls -la','pwd','find . -name test_*.py',
                        'find . -name "*.tmp" -delete','find . -type f -exec rm {} ;',
                        'python3 -c "print(1 + 1)"','node -e "console.log(2)"',
                        'bash scripts/check.sh','mkdir -p tests/tmp','git status --short',
                        'npm install','python3 -m pip install --target .deps example',
                        'custom-check --quick','curl https://example.com','rm scratch.txt',
                        'dotnet publish -c Release','chmod -R u+x tools',
                        'python3 -m unittest discover -s tests -v','npm test','cargo build'):
            with self.subTest(command=command): self.assertEqual(decision(command),'allow')
        for command in ('git push','git -C . push origin main','git reset --hard',
                        'git clean -fd','rm -rf .','sudo command',
                        'npm publish','bun run deploy','npm install -g example',
                        'git config --global user.name example','gh pr merge',
                        'opencode run task','make test deploy','cat .env'):
            with self.subTest(command=command):
                self.assertEqual(decision(command),'deny')
                self.assertEqual(decision(command,[command]),'deny')
        readonly=json.loads(w.worker_env(True,['npm test'])['OPENCODE_CONFIG_CONTENT'])['agent']['codex-worker']['permission']
        self.assertEqual(readonly['bash'],'deny');self.assertEqual(readonly['edit'],'deny')

    def test_informational_opencode_commands_allowed_only(self):
        import re
        def decision(command, extras=()):
            rules=json.loads(w.worker_env(False,extras)['OPENCODE_CONFIG_CONTENT'])['agent']['codex-worker']['permission']['bash']
            result=None
            for pattern,action in rules.items():
                regex=re.escape(pattern).replace(r'\*','.*').replace(r'\?','.')
                if re.fullmatch(regex,command,re.S): result=action
            return result
        # Harmless discovery is permitted last-match; run/serve/config stay denied.
        for command in ('opencode --help','opencode -h','opencode --version','opencode -v'):
            with self.subTest(command=command): self.assertEqual(decision(command),'allow')
        for command in ('opencode run task','opencode serve','opencode config get','opencode auth login',
                        'opencode debug agent x','opencode --help run','opencode models'):
            with self.subTest(command=command):
                self.assertEqual(decision(command),'deny')
                self.assertEqual(decision(command,[command]),'deny')
        # Last-match ordering: informational allows must follow the broad deny.
        rules=list(json.loads(w.worker_env()['OPENCODE_CONFIG_CONTENT'])['agent']['codex-worker']['permission']['bash'])
        self.assertGreater(rules.index('opencode --help'),rules.index('opencode *'))
        # Normal development tools remain allowed.
        for command in ('ls -la','npm test','python3 -m unittest discover -s tests -v'):
            self.assertEqual(decision(command),'allow')

    def test_permissions(self):
        env=w.worker_env(False,['python3 -m unittest discover'])
        agent=json.loads(env['OPENCODE_CONFIG_CONTENT'])['agent']['codex-worker']
        self.assertNotIn('model',agent)
        self.assertEqual(agent['permission']['bash']['*'],'allow')
        self.assertEqual(agent['permission']['task'],'deny')
        self.assertEqual(agent['permission']['external_directory'],'deny')
        readonly=json.loads(w.worker_env(True)['OPENCODE_CONFIG_CONTENT'])['agent']['codex-worker']
        self.assertEqual(readonly['permission']['edit'],'deny')
        self.assertEqual(readonly['permission']['bash'],'deny')

    def test_read_only_fallback_and_no_discovery(self):
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as t:
            task=Path(t)/'task'; task.write_text('task')
            a=Namespace(model=None,use_opencode_default=False,no_fallback=False,task_file=str(task),read_only=True,timeout=5,config=Path(t)/'cfg')
            read={'type':'tool_use','part':{'tool':'read','state':{'status':'completed'}}}
            error={'type':'error','error':{'name':'ModelNotFoundError'}}
            done={'type':'step_finish','part':{'reason':'stop'}}
            with patch.object(w,'CONFIG',Path(t)/'config.json'),patch.object(w,'models',side_effect=AssertionError('No discovery for normal run')),patch.object(w.streaming,'run',side_effect=[stream_result(0,json.dumps(read)+'\n'+json.dumps(error)),stream_result(0,json.dumps(done))]) as invoke:
                result=w.run_worker(a,{**w.BASE,'default':'removed/model','fallbacks':['b/b']},t)
                self.assertEqual(invoke.call_count,2)
                self.assertEqual(result['status'],'completed_needs_review')
                self.assertIn('--agent',invoke.call_args.args[0])

    def test_no_replay_after_work(self):
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as t:
            task=Path(t)/'task';task.write_text('task')
            a=Namespace(model='a/a',use_opencode_default=False,no_fallback=False,task_file=str(task),read_only=False,timeout=5,config=Path(t)/'cfg')
            events=[{'type':'tool_use','part':{'tool':'edit','state':{'status':'error'}}},{'type':'error','error':{'name':'APIError','data':{'statusCode':503}}}]
            with patch.object(w,'CONFIG',Path(t)/'config.json'),patch.object(w.streaming,'run',return_value=stream_result(0,'\n'.join(map(json.dumps,events)))) as invoke:
                result=w.run_worker(a,{**w.BASE,'fallbacks':['b/b']},t,['a/a','b/b'])
                self.assertEqual(invoke.call_count,1)
                self.assertEqual(result['status'],'provider_failure')

if __name__=='__main__': unittest.main()
