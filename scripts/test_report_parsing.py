"""Historical prose failures regress against the tool protocol, not a text parser."""
import json, unittest
import streaming
import worker as w
import submission

ACTUAL_FAILED_RESPONSE = 'All requirements are implemented and validated. Final test run: 22 tests passed.\n\n{"status":"completed","changed":["README.md","checkout/__main__.py","checkout/pricing.py","checkout/validation.py","tests/test_checkout.py"],"validation":["python3 -m unittest discover -s tests -v: 22 passed, 0 failed (OK)"],"risk":"none"}'
REPORT={'status':'completed','changed':['README.md','checkout/__main__.py','checkout/pricing.py','checkout/validation.py','tests/test_checkout.py'],'validation':['python3 -m unittest discover -s tests -v: 22 passed, 0 failed (OK)'],'risk':'none'}

class HistoricalReportTests(unittest.TestCase):
 def test_historical_prose_and_link_do_not_break_accepted_submission(self):
  for text in [ACTUAL_FAILED_RESPONSE, '[BenchEvidence/validation.md](filePath:///proof)\n'+json.dumps(REPORT), '```json\n'+json.dumps(REPORT)+'\n```']:
   with self.subTest(text=text):
    x=streaming.Summary(w.safe_to_replay)
    x.consume({'type':'tool_use','sessionID':'s','part':{'tool':submission.TOOL,'callID':'submit','state':{'status':'completed','output':json.dumps({'accepted':True,'report':REPORT})}}})
    x.consume({'type':'text','sessionID':'s','part':{'text':text}})
    self.assertEqual(x.submission.report,REPORT)
 def test_historical_text_alone_cannot_claim_success(self):
  x=streaming.Summary(w.safe_to_replay)
  x.consume({'type':'text','sessionID':'s','part':{'text':ACTUAL_FAILED_RESPONSE}})
  self.assertIsNone(x.submission.report)
