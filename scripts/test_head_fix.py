"""CLI-level regression for the explicit one-shot exception; runtime is mocked."""
import contextlib,copy,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import worker as w

class HeadFixTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.base=Path(self.tmp.name);self.repo=self.base/'repo';self.repo.mkdir()
  self.config=self.base/'config.json';self.config.write_text(json.dumps({'default':'provider/model','variants':{'provider/model':'max'}}))
  self.brief=self.base/'brief.txt';self.brief.write_text('Implement REQUEST.md and test it.')
  self.report={'status':'completed','changed':['value.py'],'validation':['unittest: 2 passed'],'risk':'none'}
  self.fake={'status':'completed_needs_review','attempts':[{'session_id':'session-original','route':'provider/model','variant':'max','status':'completed_needs_review','tools':{'edit':1},'result_submission':{'report':self.report,'error':None}}]}
 def call(self,fix=None,runtime=None,brief=None,extra=()):
  if brief is not None:self.brief.write_text(brief)
  args=['worker','--config',str(self.config),'--project',str(self.repo),'run','--brief',str(self.brief),'--evidence-dir',str(self.base/'evidence'),*extra]
  if fix:args+=['--fix-from',str(fix)]
  stdout=io.StringIO()
  with patch('sys.argv',args),patch.object(w,'CONFIG',self.base/'runtime/config.json'),patch.object(w,'run_worker',side_effect=runtime or (lambda *a:copy.deepcopy(self.fake))) as worker,contextlib.redirect_stdout(stdout):
   w.main()
  return json.loads(stdout.getvalue()),worker.call_count
 def original(self):
  result,count=self.call();self.assertEqual(count,1)
  return Path(result['evidence_dir'])/'writer.json'
 def test_normal_completes_once_without_correction(self):
  result,count=self.call();self.assertEqual(result['status'],'completed');self.assertEqual(count,1)
  self.assertNotIn('correction_used',result);self.assertFalse(list(self.base.rglob('head-fix-used')))
 def test_confirmed_bug_one_correction_then_complete(self):
  prior=self.original();source=self.repo/'value.py';source.write_text('value = -1\n')
  self.assertEqual(source.read_text(),'value = -1\n') # Head-observed concrete mismatch.
  def fix(a,d,root):
   self.assertEqual(Path(a.task_file).read_text(),'Observed value=-1; expected value=1. Fix it and run relevant tests.')
   self.assertEqual(d['default'],'provider/model');self.assertEqual(d['variants']['provider/model'],'max')
   self.assertTrue(a._submit_result);self.assertTrue(a._lock_held)
   with self.assertRaises(w.Failure):
    with w.execution_lock(root):pass
   source.write_text('value = 1\n');return copy.deepcopy(self.fake)
  result,count=self.call(prior,runtime=fix,brief='Observed value=-1; expected value=1. Fix it and run relevant tests.')
  self.assertEqual(count,1);self.assertEqual(result['status'],'completed');self.assertTrue(result['correction_used']);self.assertEqual(source.read_text(),'value = 1\n')
  evidence=json.loads((Path(result['evidence_dir'])/'writer.json').read_text());self.assertEqual(evidence['correction_of'],str(prior))
  with patch.object(w,'run_worker',side_effect=AssertionError('must not launch')):
   with self.assertRaises(w.Failure):self.call(prior,runtime=lambda *a: self.fail('second correction launched'))
  with self.assertRaises(w.Failure):self.call(Path(result['evidence_dir'])/'writer.json',runtime=lambda *a:self.fail('correction chain launched'))
 def test_execution_failure_spends_slot(self):
  prior=self.original()
  with self.assertRaises(w.Failure):self.call(prior,runtime=lambda *a:(_ for _ in ()).throw(w.Failure('execution_failed','runtime failed')))
  with self.assertRaises(w.Failure):self.call(prior,runtime=lambda *a:self.fail('replayed correction'))
 def test_unresolved_correction_stops_without_repeat(self):
  prior=self.original();self.report['status']='needs_escalation';self.report['risk']='Known input still fails'
  with self.assertRaises(SystemExit):self.call(prior)
  with self.assertRaises(w.Failure):self.call(prior,runtime=lambda *a:self.fail('second correction'))
 def test_different_project_and_read_only_block_before_launch(self):
  prior=self.original();other=self.base/'other';other.mkdir();self.repo=other
  with self.assertRaises(w.Failure):self.call(prior,runtime=lambda *a:self.fail('wrong project'))
  self.repo=self.base/'repo'
  with self.assertRaises(w.Failure):self.call(prior,extra=['--read-only'],runtime=lambda *a:self.fail('read-only correction'))
 def test_transport_failure_is_not_a_fix_trigger(self):
  prior=self.original();old=json.loads(prior.read_text());old['attempts'][0]['result_submission']={'report':None,'error':'missing_submission'};prior.write_text(json.dumps(old))
  with self.assertRaises(w.Failure):self.call(prior,runtime=lambda *a:self.fail('transport replay'))
  self.assertFalse((prior.parent/'head-fix-used').exists())
 def test_empty_brief_does_not_consume_slot(self):
  prior=self.original()
  with self.assertRaises(w.Failure):self.call(prior,brief=' ')
  self.assertFalse((prior.parent/'head-fix-used').exists())
 def test_suspicion_and_risk_do_not_automatically_launch_another_worker(self):
  self.report['risk']='Boundary behavior needs confirmation'
  result,count=self.call();self.assertEqual(count,1);self.assertNotIn('correction_used',result)
if __name__=='__main__':unittest.main()
