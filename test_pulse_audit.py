import copy
import tempfile
import unittest
from unittest.mock import AsyncMock
from application_intake import PulseAudit
from checklist_workflow import ChecklistWorkflow
from test_application_intake import Board, Source, NOW, IntakeTests
from test_automatic_followup import person, change


class AuditBoard:
    def __init__(self):
        self.updates = []
        self.posts = 0
        self.fail_read = False
        self.uncertain = False
        self.record_post = True
        self.board_id = '424237712'

    async def call(self, query, variables=None):
        if query.startswith('query'):
            if self.fail_read:
                raise RuntimeError('read failed')
            return {'items':[{'id':variables['ids'][0], 'board':{'id':self.board_id}, 'updates':copy.deepcopy(self.updates)}]}
        if 'create_update' in query:
            self.posts += 1
            row = {'id':str(1000+self.posts), 'body':variables['body']}
            if self.record_post:
                self.updates.append(row)
            if self.uncertain:
                raise RuntimeError('response lost')
            return {'create_update':{'id':row['id']}}
        raise AssertionError(query)


class AuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.board = AuditBoard()
        self.worker = ChecklistWorkflow(self.temp.name+'/audit.sqlite', self.board, sender=AsyncMock())
        self.audit = PulseAudit(self.worker)

    async def asyncTearDown(self):
        self.worker.ledger.db.close()
        self.temp.cleanup()

    async def test_unchanged_cycles_and_restart_do_not_duplicate_posts(self):
        self.audit.snapshot('90001','intake','0/5 forms received. Next reminder tomorrow.')
        await self.audit.flush()
        self.audit = PulseAudit(self.worker)
        self.audit.snapshot('90001','intake','0/5 forms received. Next reminder tomorrow.')
        await self.audit.flush()
        self.assertEqual(self.board.posts,1)
        self.assertIn('baseline',self.board.updates[0]['body'])
        self.assertEqual(self.worker.state['pulseAudit']['posted'],1)

    async def test_changes_and_return_to_previous_state_are_both_recorded(self):
        for message in ['Active','Paused','Active']:
            self.audit.snapshot('90001','intake',message)
        await self.audit.flush()
        self.assertEqual(self.board.posts,3)
        self.assertIn('Workflow position changed',self.board.updates[-1]['body'])

    async def test_provider_accepted_but_response_lost_is_reconciled(self):
        self.audit.event('90001','send:1','Reminder accepted; delivery not confirmed.')
        self.board.uncertain=True
        await self.audit.flush()
        self.assertEqual(self.worker.state['pulseAudit']['uncertain'],1)
        self.audit=PulseAudit(self.worker)
        await self.audit.flush()
        self.assertEqual(self.board.posts,1)
        self.assertEqual(self.worker.state['pulseAudit']['posted'],1)

    async def test_uncertain_absent_post_is_not_blindly_resent(self):
        self.audit.event('90001','send:1','Delivery outcome uncertain.')
        self.board.uncertain=True
        self.board.record_post=False
        await self.audit.flush()
        await self.audit.flush()
        self.assertEqual(self.board.posts,1)
        self.assertEqual(self.worker.state['pulseAudit']['uncertain'],1)

    async def test_read_failure_retries_and_foreign_board_never_posts(self):
        self.audit.snapshot('90001','intake','Active')
        self.board.fail_read=True
        await self.audit.flush()
        self.assertEqual(self.board.posts,0)
        self.board.fail_read=False
        self.board.board_id='123'
        await self.audit.flush()
        self.assertEqual(self.board.posts,0)
        self.board.board_id='424237712'
        await self.audit.flush()
        self.assertEqual(self.board.posts,1)

    async def test_applicant_baseline_excludes_employed_and_rejected(self):
        candidate=person('pre_employment')
        employed=person()
        employed['id']='900000002'
        rejected=person('pre_employment')
        rejected['id']='900000003'
        change(rejected,'status53','Rejected through process')
        self.audit.applicants([candidate,employed,rejected])
        self.audit.applicants([candidate,employed,rejected])
        await self.audit.flush()
        self.assertEqual(self.board.posts,1)
        self.assertIn('not independent verification',self.board.updates[0]['body'])

    async def test_event_text_is_escaped(self):
        self.audit.event('90001','example','<script>test</script>')
        await self.audit.flush()
        self.assertIn('&lt;script&gt;',self.board.updates[0]['body'])


class IntakeAuditTests(IntakeTests):
    async def test_intake_receipt_and_reminder_events_survive_restart(self):
        self.worker.audit = PulseAudit(self.worker)
        await self.run_cycle()
        self.assertEqual(self.worker.ledger.db.execute('SELECT count(*) FROM pulse_audit_event').fetchone()[0],2)
        await self.run_cycle(NOW)
        self.assertEqual(self.worker.ledger.db.execute('SELECT count(*) FROM pulse_audit_event').fetchone()[0],2)
        from datetime import timedelta
        await self.run_cycle(NOW+timedelta(days=3))
        bodies=[r['body'] for r in self.worker.ledger.db.execute('SELECT body FROM pulse_audit_event')]
        self.assertTrue(any('Reminder 1/3 accepted' in body for body in bodies))
