import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from argparse import Namespace
from unittest.mock import patch

import worker as w


def stream_result(rc,text,err=''):
    s=w.streaming.Summary(w.safe_to_replay)
    for line in text.splitlines(): s.consume(json.loads(line))
    return rc,s,bool(err),None


class DiscoveryFixturesTests(unittest.TestCase):
    def test_python_node_plain_repos_are_generic(self):
        for marker in (('pyproject.toml','src/app.py'),('package.json','src/index.js'),('AGENTS.md','lib/a.rs')):
            with tempfile.TemporaryDirectory() as t:
                root=Path(t)
                for name in marker: (root/name).parent.mkdir(parents=True,exist_ok=True); (root/name).write_text('x')
                ctx=w.discover_context(root,home=root)
                # No Unity markers required; discovery is purely generic.
                self.assertIn('instructions',ctx);self.assertIn('shared_skills',ctx)
                self.assertEqual(ctx['shared_skills'],[])

    def test_unity_project_is_generic_parity_fixture(self):
        # A Unity-marked repo receives no special controller logic: its installed
        # originals are exposed read-only exactly like any other shared skill.
        with tempfile.TemporaryDirectory() as t:
            home=Path(t);root=home/'game';root.mkdir()
            for name in ('Assets','Packages','ProjectSettings'):(root/name).mkdir()
            (root/'Packages/manifest.json').write_text('{}')
            (root/'ProjectSettings/ProjectVersion.txt').write_text('6000')
            skill=home/'.agents/skills/manage-unity-workflows';skill.mkdir(parents=True);(skill/'SKILL.md').write_text('original')
            with patch.object(w.Path,'home',return_value=home):
                ctx=w.discover_context(root)
                perms=json.loads(w.worker_env(shared_roots=ctx['shared_roots'])['OPENCODE_CONFIG_CONTENT'])['agent']['codex-worker']['permission']
            root_path=str((home/'.agents/skills').resolve())
            self.assertEqual([s['name'] for s in ctx['shared_skills']],['manage-unity-workflows'])
            self.assertEqual(perms['external_directory'][root_path],'allow')
            self.assertEqual(perms['read'][root_path+'/*'],'allow')
            self.assertEqual(perms['edit'][root_path+'/*'],'deny')
    def test_agents_md_and_project_skills_discovered(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            (root/'AGENTS.md').write_text('follow')
            skill=root/'.agents/skills/frontend';skill.mkdir(parents=True);(skill/'SKILL.md').write_text('s')
            ctx=w.discover_context(root,home=root)
            self.assertEqual([i['name'] for i in ctx['instructions']],['AGENTS.md'])
            self.assertEqual([s['name'] for s in ctx['project_skills']],['frontend'])
            # Lazy: catalog exposes only paths, content is not read into context.
            self.assertNotIn('follow',json.dumps(ctx))


class RolePermissionTests(unittest.TestCase):
    def test_writer_allows_custom_tools_and_ordinary_shell(self):
        p=json.loads(w.worker_env()['OPENCODE_CONFIG_CONTENT'])['agent']['codex-worker']['permission']
        self.assertEqual(p['*'],'allow')
        self.assertEqual(p['bash']['*'],'allow')
        for command in ('find . -name x -delete','find . -exec rm {} ;','dotnet publish -c Release','chmod -R u+x tools'):
            self.assertNotIn(command,p['bash'])

    def test_read_only_denies_all_mutating_and_unknown_tools(self):
        p=json.loads(w.worker_env(read_only=True)['OPENCODE_CONFIG_CONTENT'])['agent']['codex-worker']['permission']
        # Explicit mutating tools are denied; every other named or custom/MCP tool
        # is denied by the catch-all '*', leaving only native reads.
        for name in ('edit','bash','write','patch','task'):
            self.assertEqual(p.get(name),'deny',name)
        self.assertEqual(p['*'],'deny')
        for name in ('glob','grep','list'):
            self.assertEqual(p[name],'allow',name)
        self.assertEqual(p['read']['*'],'allow')

    def test_writer_shared_roots_readonly(self):
        p=json.loads(w.worker_env(shared_roots=['/orig/skill'])['OPENCODE_CONFIG_CONTENT'])['agent']['codex-worker']['permission']
        self.assertEqual(p['read']['/orig/skill/*'],'allow')
        self.assertEqual(p['edit']['/orig/skill/*'],'deny')


class TimeoutEvidenceTests(unittest.TestCase):
    def _args(self,t):
        task=Path(t)/'task';task.write_text('task')
        return Namespace(model='a/a',use_opencode_default=False,no_fallback=True,task_file=str(task),
            read_only=True,timeout=5,config=Path(t)/'cfg')

    def test_oserror_after_launch_keeps_evidence_and_is_interrupted(self):
        with tempfile.TemporaryDirectory() as t:
            summary=w.streaming.Summary(w.safe_to_replay)
            summary.consume({'type':'text','sessionID':'sess-1','part':{'text':'partial work'}})
            summary.consume({'type':'step_finish','sessionID':'sess-1','part':{'id':'p','reason':'tool-calls','tokens':{'input':7,'output':2}}})
            error=OSError('read failed after start');error.worker_summary=summary;error.worker_launched=True
            with patch.object(w,'CONFIG',Path(t)/'cfg'),patch.object(w.streaming,'run',side_effect=error):
                result=w.execution_evidence(w.run_worker(self._args(t),w.BASE,t))
            self.assertEqual(result['status'],'interrupted')
            self.assertNotEqual(result['attempts'][0]['status'],'launch_failed')
            self.assertEqual(result['attempts'][0]['session_id'],'sess-1')
            self.assertTrue(result['worker_used'])
            self.assertEqual(result['worker_tokens_all_attempts']['input'],7)

    def test_oserror_before_launch_is_launch_failed(self):
        with tempfile.TemporaryDirectory() as t:
            error=OSError('spawn failed');error.worker_launched=False
            with patch.object(w,'CONFIG',Path(t)/'cfg'),patch.object(w.streaming,'run',side_effect=error):
                result=w.execution_evidence(w.run_worker(self._args(t),w.BASE,t))
            self.assertEqual(result['status'],'launch_failed')
            self.assertFalse(result['worker_process_started'])

    def test_streaming_cleanup_oserror_still_raises_with_evidence(self):
        with tempfile.TemporaryDirectory() as t:
            exe=Path(t)/'opencode'
            exe.write_text('#!/usr/bin/env python3\nimport time\ntime.sleep(10)\n');exe.chmod(0o700)
            env={**os.environ,'PATH':t+os.pathsep+os.environ['PATH']}
            with self.assertRaises(TimeoutError) as caught:
                w.streaming.run(['run'],t,0.1,'task',env,w.safe_to_replay)
            self.assertTrue(hasattr(caught.exception,'worker_summary'))
            self.assertTrue(hasattr(caught.exception,'worker_log'))


class CompactOutputTests(unittest.TestCase):
    def _rich(self):
        return {'status':'completed_needs_review','worker_used':True,
            'attempts':[{'role':'writer','route':'p/w','status':'completed_needs_review','session_id':'s1',
                'worker_tokens':{'reported':{'input':10,'output':2},'coverage':'observed_step_events'},
                'shell_commands':[{'command':'pytest -q','exit':0}],
                'mutation_files_reported_by_tools':['a.py','b.py'],
                'worker_answer':'Implemented feature safely and completely.'}],
            'changed_files':['a.py'],'evidence_dir':'/private/ev'}

    def test_compact_omits_commands_and_metadata(self):
        out=w.compact_output(self._rich())
        text=w.encode_result(out,compact=True)
        for leaked in ('shell_commands','mutation_files_reported_by_tools'):
            self.assertNotIn(leaked,text)
        # The size field itself is retained intentionally.
        self.assertEqual(json.loads(text)['returned_bytes'],len(text.encode()))
        self.assertIn('Implemented feature',text)
        self.assertLess(len(text.encode()),2500)

    def test_compact_keeps_evidence(self):
        out=w.compact_output(self._rich())
        self.assertTrue(out['worker_used']);self.assertEqual(out['changed_files'],['a.py'])
        self.assertIn('evidence_dir',out)

    def test_compact_large_input_is_bounded(self):
        result=self._rich()
        result['changed_files']=['src/deep/path/file_%04d.py'%i for i in range(500)]
        result['changed_files']+=[('p'*500)+str(i) for i in range(20)]
        out=w.compact_output(result);text=w.encode_result(out,compact=True)
        self.assertLess(len(text.encode()),12000)
        # Bounded path sample plus explicit omission count; no unbounded arrays.
        self.assertEqual(out['changed_file_count'],len(result['changed_files']))
        self.assertLessEqual(len(out['changed_files']),w.COMPACT_CHANGED_FILE_SAMPLE)
        self.assertEqual(out['changed_files_omitted'],len(result['changed_files'])-w.COMPACT_CHANGED_FILE_SAMPLE)
        for path in out['changed_files']: self.assertLessEqual(len(path),w.COMPACT_PATH_CHARS)
        # Full details remain in private evidence, not the compact view.
        for leaked in ('shell_commands','mutation_files_reported_by_tools'):
            self.assertNotIn(leaked,text)

    def test_compact_attempt_summary_is_bounded(self):
        result=self._rich()
        result['attempts'][0]['worker_answer']='y'*8000
        out=w.compact_output(result)
        self.assertLessEqual(len(out['attempts'][0]['summary']),w.COMPACT_SUMMARY_CHARS)
        self.assertLess(len(w.encode_result(out,compact=True).encode()),3000)

    def test_full_output_preserved(self):
        payload=w.encode_result(self._rich())
        self.assertIn('shell_commands',payload)


if __name__=='__main__':
    unittest.main()
