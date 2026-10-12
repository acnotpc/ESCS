import os,tempfile,unittest
from unittest.mock import AsyncMock,patch
from automatic_followup import AutomaticSelection
from history_email_example import example_analysis,body,send_example
from checklist_workflow import ChecklistWorkflow
from test_automatic_followup import person,AutoBoard
from test_extended_followup import ExtendedSource
from test_checklist_workflow import NOW
class Tests(unittest.IsolatedAsyncioTestCase):
    def test_actual_engine_identifies_synthetic_gaps(self):
        issues=example_analysis()['issues']
        self.assertTrue(any('2019-07-01 to 2019-08-31' in x for x in issues))
        self.assertTrue(any('2023-01-01 to 2023-02-28' in x for x in issues))
        self.assertTrue(any('entry 2' in x and 'contact' in x for x in issues))
        self.assertIn('TEST EXAMPLE ONLY',body());self.assertIn('<a href=',body());self.assertIn('not an assessment of your own record',body())
    async def test_once_and_unknown_without_staff_changes(self):
        for failed in [False,True]:
            with tempfile.TemporaryDirectory() as folder:
                r=person();b=AutoBoard([r]);s=AsyncMock()
                if failed:s.send.side_effect=RuntimeError('unknown')
                w=ChecklistWorkflow(folder+'/db',b,ExtendedSource(),s);a=AutomaticSelection(w)
                with patch.dict(os.environ,{'HR_HISTORY_EMAIL_EXAMPLE_ID':'synthetic-test','HR_HISTORY_EMAIL_EXAMPLE_ITEM':r['id']}):
                    await send_example(a,[r],NOW);await send_example(a,[r],NOW)
                self.assertEqual(s.send.await_count,1);self.assertEqual(s.send.await_args.args[0],'employed')
                self.assertFalse(b.writes);self.assertFalse(w.ledger.db.execute('SELECT * FROM checklist_candidate').fetchall());w.ledger.db.close()
