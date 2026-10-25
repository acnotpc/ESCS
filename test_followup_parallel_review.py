"""Regression tests for guarded expansion of an existing unsent follow-up campaign.

Run: python -m unittest test_followup_parallel_review -v
"""
import json
import os
os.environ['HR_FOLLOWUP_EXTENDED_FORMS'] = 'true'
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from checklist_workflow import ChecklistWorkflow, REQUEST
from test_checklist_workflow import Board, Source, record
import tempfile

NOW = datetime(2026, 10, 25, 16, tzinfo=timezone.utc)
CHANGE_REASON = 'Email, stage or requested items changed. Verify them, then tick HR follow-up resume.'

class ParallelReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.board = Board(record((1, 2, 4)))
        self.worker = ChecklistWorkflow(self.tmp.name + '/followup.sqlite', self.board, Source(), AsyncMock())
        self.worker.reconcile(self.board.current, NOW, enroll=True)

    async def asyncTearDown(self):
        self.worker.ledger.db.close()
        self.tmp.cleanup()

    def expand(self):
        column = next(c for c in self.board.current['column_values'] if c['id'] == REQUEST)
        column['value'] = json.dumps({'ids': [1, 2, 4, 11, 12, 13, 14, 15]})

    def campaign(self):
        return self.worker.ledger.db.execute(
            'SELECT state,sends,next_due FROM checklist_candidate WHERE id=?', ('1005891310',)).fetchone()

    async def test_unsent_expansion_preserves_clock_and_is_idempotent(self):
        before = self.campaign()
        self.expand()
        self.worker.reconcile(self.board.current, NOW)
        self.worker.reconcile(self.board.current, NOW)
        after = self.campaign()
        self.assertEqual(after['sends'], before['sends'])
        self.assertEqual(after['next_due'], before['next_due'])
        count = self.worker.ledger.db.execute(
            'SELECT count(*) FROM checklist_requirement WHERE candidate_id=?', ('1005891310',)).fetchone()[0]
        self.assertEqual(count, 8)

    async def test_request_change_hold_can_reconcile_without_reset(self):
        self.expand()
        self.worker.hold('1005891310', CHANGE_REASON)
        self.worker.reconcile(self.board.current, NOW)
        self.assertEqual(self.campaign()['state'], 'active')

    async def test_unrelated_hold_is_not_released(self):
        self.expand()
        self.worker.hold('1005891310', 'Linked form response needs staff review.')
        with self.assertRaises(ValueError):
            self.worker.reconcile(self.board.current, NOW)
        self.assertEqual(self.campaign()['state'], 'staff_review')

    async def test_uncertain_delivery_stays_held(self):
        self.expand()
        self.worker.ledger.db.execute(
            'INSERT INTO checklist_attempt(candidate_id,attempted_at,status) VALUES(?,?,?)',
            ('1005891310', NOW.isoformat(), 'unknown'))
        self.worker.ledger.db.commit()
        with self.assertRaises(ValueError):
            self.worker.reconcile(self.board.current, NOW)
        self.assertEqual(self.campaign()['state'], 'staff_review')

    async def test_already_sent_campaign_does_not_expand(self):
        self.expand()
        self.worker.ledger.db.execute(
            'UPDATE checklist_candidate SET sends=1 WHERE id=?', ('1005891310',))
        self.worker.ledger.db.commit()
        with self.assertRaises(ValueError):
            self.worker.reconcile(self.board.current, NOW)
        self.assertEqual(self.campaign()['state'], 'staff_review')

if __name__ == '__main__':
    unittest.main()
