import tempfile
import unittest
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs
from recruitment_checklist import Checklist
from recruitment_tracking import TrackingLinks, ReceiptHold
from test_recruitment_receipts import sample


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + '/checks.sqlite'
        self.ledger = Checklist(self.path)
        self.now = datetime.now(timezone.utc)
        self.ledger.request('123', ['medical'], self.now, verified_link=True, staff_requested=True)
        self.links = TrackingLinks(self.ledger)
        self.schema, self.record = sample()
        self.schema['10'] = {'qid': '10', 'text': 'ESCS follow-up request ID',
            'type': 'control_textbox', 'hidden': 'Yes', 'required': 'No', 'name': 'escsFollowupRequestId'}

    def tearDown(self):
        self.ledger.db.close()
        self.tmp.cleanup()

    def token(self):
        url = self.links.issue('123', 'medical', self.schema)
        self.assertNotIn('candidate', url)
        self.assertEqual('form.jotform.com', urlsplit(url).hostname)
        return parse_qs(urlsplit(url).query)['escsFollowupRequestId'][0]

    def add_answer(self, token):
        self.record['answers']['10'] = {'qid': '10', 'text': 'ESCS follow-up request ID',
                                       'type': 'control_textbox', 'answer': token}

    def test_issued_link_matches_and_stops(self):
        token = self.token()
        self.add_answer(token)
        self.assertTrue(self.links.accept(self.schema, self.record))
        self.assertEqual([], self.ledger.plan('123', self.now))
        self.assertFalse(self.links.accept(self.schema, self.record))

    def test_link_survives_restart_and_is_idempotent(self):
        token = self.token()
        self.ledger.db.close()
        self.ledger = Checklist(self.path)
        self.links = TrackingLinks(self.ledger)
        self.assertEqual(token, self.token())
        self.add_answer(token)
        self.assertTrue(self.links.accept(self.schema, self.record))

    def test_unknown_token_never_stops(self):
        self.add_answer('a'*32)
        with self.assertRaises(ReceiptHold):
            self.links.accept(self.schema, self.record)
        self.assertEqual(['medical'], self.ledger.plan('123', self.now))

    def test_wrong_form_never_stops(self):
        self.add_answer(self.token())
        self.record['form_id'] = '211923425771051'
        with self.assertRaises(ReceiptHold):
            self.links.accept(self.schema, self.record)

    def test_cannot_issue_for_unrequested_candidate(self):
        with self.assertRaises(ReceiptHold):
            self.links.issue('999', 'medical', self.schema)

    def test_tracking_schema_drift_blocks(self):
        self.schema['10']['hidden'] = 'No'
        with self.assertRaises(ReceiptHold):
            self.links.issue('123', 'medical', self.schema)
