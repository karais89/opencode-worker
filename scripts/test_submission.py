import json, os, subprocess, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import submission as s
import streaming
import worker as w

REPORT={'status':'completed','changed':['labels.py'],'validation':['unittest: 2 passed'],'risk':'none'}

def event(report=REPORT, call='one', status='completed', session='ses-one', output=None, tool=s.TOOL):
    return {'type':'tool_use','sessionID':session,'part':{'tool':tool,'callID':call,'state':{'status':status,'input':{},'output':output if output is not None else json.dumps({'accepted':True,'report':report})}}}

def summary(*events):
    result=streaming.Summary(w.safe_to_replay)
    for e in events:result.consume(e)
    return result

class SubmissionTests(unittest.TestCase):
    def setUp(self):
        # Hermetic SDK surface fixture, not a live OpenCode/SDK integration test.
        # Exercise the actual plugin execute() validator without an installed CLI,
        # provider credentials, network, or changes to user configuration.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config = Path(temporary.name) / 'config'
        package = config / 'node_modules/@opencode-ai/plugin'
        (package / 'dist').mkdir(parents=True)
        (package / 'package.json').write_text('{"type":"module"}')
        (package / 'dist/tool.js').write_text(
            'const shape = { optional() { return this; } };\n'
            'export const tool = Object.assign(x => x, { schema: {\n'
            ' string: () => shape, array: () => shape, enum: () => shape\n'
            '} });\n')
        environment = patch.dict(os.environ, {'OPENCODE_CONFIG_DIR': str(config)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_actual_markdown_prefix_and_verbose_prose_are_not_protocol(self):
        for text in ['[BenchEvidence/validation.md](filePath:///tmp/proof)\n'+json.dumps(REPORT),'```json\n'+json.dumps(REPORT)+'\n```','[arbitrary] {prose} '*500]:
            x=summary(event(),{'type':'text','sessionID':'ses-one','part':{'text':text}})
            self.assertEqual(x.public()['result_submission']['report'],REPORT)
        x=summary({'type':'text','sessionID':'ses-one','part':{'text':json.dumps(REPORT)}})
        self.assertIsNone(x.submission.report)

    def test_bad_then_good_same_session(self):
        x=summary(event(call='bad',status='error'),event(call='good'))
        self.assertEqual(x.submission.report,REPORT)
        self.assertEqual(x.tools[s.TOOL],2)
        self.assertEqual(x.tool_status[s.TOOL],{'completed':1,'error':1})

    def test_duplicate_conflict_and_later_failure(self):
        x=summary(event(),event(),event(call='two'))
        self.assertEqual(x.submission.report,REPORT)
        x.consume(event({**REPORT,'risk':'unverified'},call='three'))
        self.assertIsNone(x.submission.report)
        self.assertEqual(x.submission.error,'conflicting_submissions')
        x=summary(event(),event(call='bad',status='error'))
        self.assertIsNone(x.submission.report)
        self.assertEqual(x.submission.error,'submission_rejected')

    def test_late_development_requires_new_submission(self):
        x=summary(event(),event(tool='bash',call='test'))
        self.assertIsNone(x.submission.report)
        x.consume(event(call='new'))
        self.assertEqual(x.submission.report,REPORT)

    def test_session_mismatch_not_accepted(self):
        x=summary({'type':'step_start','sessionID':'ses-one'},event(session='ses-other'))
        self.assertTrue(x.invalid)
        self.assertIsNone(x.submission.report)

    def test_field_validation_and_partial_envelope(self):
        bad=[{k:v for k,v in REPORT.items() if k!=missing} for missing in REPORT]
        bad += [{**REPORT,k:v} for k,v in [('status','pass'),('changed','x'),('changed',['']),('validation',[]),('validation',[' ']),('risk',''),('risk',None),('risk','x'*1800)]]
        for report in bad:
            with self.subTest(report=report):
                with self.assertRaises(ValueError):s.validate(report)
                self.assertIsNone(summary(event(report)).submission.report)
        self.assertIsNone(summary(event(output='{"accepted":true')).submission.report)
        self.assertIsNone(summary(event(output='[]')).submission.report)

    def test_worker_failure_still_blocks_and_usage_preserved(self):
        x=summary(event(),{'type':'step_finish','sessionID':'ses-one','part':{'id':'step-1','reason':'stop','tokens':{'input':4,'output':3,'reasoning':0,'cache':{'read':0,'write':0},'total':7}}})
        for execution in ['completed_needs_review','execution_failed']:
            with tempfile.TemporaryDirectory() as t:
                fake={'status':execution,'attempts':[{'status':execution,**x.public()}]}
                with patch.object(w,'run_worker',return_value=fake):
                    out=w.run_direct(SimpleNamespace(evidence_dir=str(Path(t)/'evidence')),{},str(Path(t)/'repo'))['public_result']
                self.assertEqual(out['status'],'completed' if execution=='completed_needs_review' else 'needs_escalation')
                self.assertEqual(out['worker']['reported_tokens'],7)
                if execution!='completed_needs_review':self.assertEqual(out['failure_kind'],'worker_execution_failed')

    def test_process_only_plugin_merge_and_permissions(self):
        env=w.worker_env()
        original=json.loads(env['OPENCODE_CONFIG_CONTENT'])
        original['plugin']=['file:///existing-plugin.mjs']
        env['OPENCODE_CONFIG_CONTENT']=json.dumps(original)
        s.configure(env);s.configure(env)
        result=json.loads(env['OPENCODE_CONFIG_CONTENT'])
        self.assertEqual(result['agent']['codex-worker']['permission'].pop(s.TOOL),'allow')
        self.assertEqual(result['agent'],original['agent'])
        self.assertEqual(len(result['plugin']),2)
        self.assertEqual(result['plugin'][0],original['plugin'][0])
        read_only=w.worker_env(True);before=json.loads(read_only['OPENCODE_CONFIG_CONTENT']);s.configure(read_only)
        after=json.loads(read_only['OPENCODE_CONFIG_CONTENT'])
        self.assertEqual(after['agent']['codex-worker']['permission'].pop(s.TOOL),'allow')
        self.assertEqual(after['agent'],before['agent'])

    def test_validation_commands_python_js_parity_with_sdk_fixture(self):
        env=w.worker_env();s.configure(env)
        plugin=(Path(__file__).resolve().parent.parent/'assets/submit-result.mjs').as_uri()
        candidates = [REPORT, {**REPORT, 'validation_commands': []},
                      {**REPORT, 'validation_commands': ['pytest -q']},
                      {**REPORT, 'validation_commands': ['x' * 256]},
                      {**REPORT, 'validation_commands': [chr(0x1f680) * 256]}]
        candidates += [{**REPORT, 'validation_commands': commands} for commands in
                       (None, 'pytest', [''], [' '], [None], ['pytest'] * 2,
                        ['x' * 257], [str(i) for i in range(11)],
                        [('x' * 200) + str(i) for i in range(10)])]
        expected=[]
        for candidate in candidates:
            try: expected.append({'accepted': True, 'report': s.validate(candidate)})
            except ValueError: expected.append({'accepted': False})
        code = """
import create from PLUGIN;
const t=(await create()).tool.codex_worker_submit_result;
const results=[];
for (const report of CANDIDATES) {
 try { results.push(JSON.parse(await t.execute(report,{}))); }
 catch { results.push({accepted:false}); }
}
console.log(JSON.stringify(results));
""".replace('PLUGIN',json.dumps(plugin)).replace('CANDIDATES',json.dumps(candidates))
        process=subprocess.run(['node','--input-type=module','-e',code],env=env,text=True,capture_output=True)
        self.assertEqual(process.returncode,0,process.stderr)
        self.assertEqual(json.loads(process.stdout),expected)

    def test_js_plugin_validator_and_same_instance_repair_with_sdk_fixture(self):
        env=w.worker_env();s.configure(env)
        plugin=(Path(__file__).resolve().parent.parent/'assets/submit-result.mjs').as_uri()
        code='''
import create from PLUGIN;
const t=(await create()).tool.codex_worker_submit_result;
const report=REPORT;
let rejected=0;
for (const bad of [{...report,validation:[""]},{...report,validation:[]},{...report,risk:""},{...report,risk:"x".repeat(1800)},{...report,status:"pass"},{...report,changed:"x"}]) {
 try { await t.execute(bad,{}); throw new Error("unexpected acceptance"); }
 catch(e) { if(e.message==="unexpected acceptance") throw e; rejected++; }
}
console.log(JSON.stringify({rejected,accepted:JSON.parse(await t.execute(report,{}))}));
'''.replace('PLUGIN',json.dumps(plugin)).replace('REPORT',json.dumps(REPORT))
        p=subprocess.run(['node','--input-type=module','-e',code],env=env,text=True,capture_output=True)
        self.assertEqual(p.returncode,0,p.stderr)
        result=json.loads(p.stdout);self.assertEqual(result['rejected'],6);self.assertEqual(result['accepted']['report'],REPORT)

if __name__=='__main__':unittest.main()
