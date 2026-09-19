import contextlib,io,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import worker as w

class LiteTests(unittest.TestCase):
 def invoke(self,brief='Goal: implement the requested option',extra=(),answer=None):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t)/'repo';root.mkdir();task=Path(t)/'brief';task.write_text(brief)
   cfg=Path(t)/'config';cfg.write_text(json.dumps({'default':'provider/model'}))
   fake={'status':'completed_needs_review','attempts':[{'session_id':'ses-test','route':'provider/model','status':'completed_needs_review','worker_answer':json.dumps({'status':'completed','changed':['a.py'],'validation':['pytest: 2 passed'],'risk':'none'}),'tools':{'edit':1},'shell_commands':[{'command':'pytest','tool_status':'completed','exit':0}]}]}
   fake['attempts'][0]['result_submission']={'report':json.loads(fake['attempts'][0]['worker_answer']),'error':None}
   if answer is not None:
    fake['attempts'][0]['worker_answer']=answer
    try:fake['attempts'][0]['result_submission']['report']=json.loads(answer)
    except ValueError:fake['attempts'][0]['result_submission']={'report':None,'error':'missing_submission'}
   argv=['worker','--config',str(cfg),'--project',str(root),'run','--brief',str(task),'--evidence-dir',str(Path(t)/'evidence'),*extra]
   output=io.StringIO()
   with patch('sys.argv',argv),patch.object(w,'run_worker',return_value=fake) as writer,contextlib.redirect_stdout(output):
    w.main()
   data=json.loads(output.getvalue());proof=next((Path(t)/'evidence').glob('*/writer.json'))
   self.assertEqual(proof.stat().st_mode & 0o777,0o600)
   if answer is not None:self.assertEqual(json.loads(proof.read_text())['attempts'][0]['worker_answer'],answer)
   self.assertEqual(writer.call_count,1)
   return data
 def test_default_is_one_writer_no_gate_no_review(self):
  data=self.invoke();self.assertEqual(data['status'],'completed');self.assertNotIn('phases',data);self.assertEqual(data['validation'],['pytest: 2 passed']);self.assertNotIn('review_required',data);self.assertLess(len(json.dumps(data)),1500)
 def test_brief_does_not_require_magic_fields(self):
  self.assertTrue(self.invoke('Add the option described in REQUEST.md; preserve compatibility and pass tests.')['worker_used'])
 def test_empty_brief_blocks_launch_cheaply(self):
  with self.assertRaises(w.Failure) as error:self.invoke(' ')
  self.assertEqual(error.exception.code,'invalid_brief')
 def test_oversized_brief_blocks_launch_cheaply(self):
  with self.assertRaises(w.Failure):self.invoke('x'*8193)

 def test_missing_or_truncated_report_does_not_claim_success(self):
  with self.assertRaises(SystemExit) as error:self.invoke(answer='x'*2300+' Risks: remaining issue')
  self.assertEqual(error.exception.code,2)
 def test_dead_review_and_contract_flags_are_removed(self):
  # Ultra Lite has no reviewer, contract gate or automatic corrective chain, so the
  # legacy CLI surface is rejected by argparse before any worker launch.
  dead=[('--with-review',),('--review-policy','auto'),('--max-corrections','1'),('--correct-from','x'),('--contract','x'),('--change-kind','feature')]
  for extra in dead:
   with self.subTest(extra=extra):
    with self.assertRaises(SystemExit) as error:self.invoke(extra=extra)
    self.assertEqual(error.exception.code,2)
 def test_reported_uncertainty_escalates(self):
  with self.assertRaises(SystemExit):self.invoke(answer=json.dumps({'status':'needs_escalation','changed':[],'validation':['not run'],'risk':'tool unavailable'}))
