import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from recruitment_checklist import Checklist, REQUIREMENTS, DBS_DETAILS, history_gaps, missing_dbs_details, message, html_message


class ChecklistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + '/checks.sqlite'
        self.ledger = Checklist(self.path)
        self.now = datetime(2026, 10, 12, tzinfo=timezone.utc)

    def tearDown(self):
        self.ledger.db.close()
        self.tmp.cleanup()

    def enroll(self, kinds=REQUIREMENTS):
        self.ledger.request('123', kinds, self.now, verified_link=True, staff_requested=True)

    def test_requires_link_and_request(self):
        with self.assertRaises(ValueError):
            self.ledger.request('123', REQUIREMENTS, self.now)

    def test_partial_receipt_removed_and_persists(self):
        self.enroll()
        self.ledger.receive('123', 'medical', '456', exact_match=True)
        self.ledger.db.close()
        self.ledger = Checklist(self.path)
        self.assertNotIn('medical', self.ledger.reserve('123', self.now))

    def test_all_received_stops_without_approval(self):
        self.enroll()
        for kind in REQUIREMENTS:
            self.ledger.receive('123', kind, '456', exact_match=True)
        self.enroll()
        self.assertEqual([], self.ledger.plan('123', self.now + timedelta(days=30)))
        self.assertEqual('received_review', self.ledger.db.execute('SELECT state FROM checklist_candidate').fetchone()[0])

    def test_wrong_receipt_does_not_stop(self):
        self.enroll(['history'])
        with self.assertRaises(ValueError):
            self.ledger.receive('123', 'history', '456')
        self.assertEqual(['history'], self.ledger.plan('123', self.now))

    def test_three_reminders_then_review(self):
        self.enroll(['history'])
        for n in range(4):
            now = self.now + timedelta(hours=72*n)
            self.assertEqual(['history'], self.ledger.reserve('123', now))
            self.ledger.delivery('123', now, confirmed=True)
            self.assertEqual([], self.ledger.plan('123', now + timedelta(hours=71)))
        self.assertEqual([], self.ledger.plan('123', self.now + timedelta(hours=288)))

    def test_uncertain_delivery_not_retried(self):
        self.enroll()
        self.ledger.reserve('123', self.now)
        self.ledger.delivery('123', self.now)
        self.assertEqual([], self.ledger.plan('123', self.now + timedelta(days=10)))

    def test_receipt_before_reservation_stops(self):
        self.enroll(['working_time'])
        self.assertEqual(['working_time'], self.ledger.plan('123', self.now))
        self.ledger.receive('123', 'working_time', '456', exact_match=True)
        self.assertEqual([], self.ledger.reserve('123', self.now))

    def test_receipt_during_send_not_reopened(self):
        self.enroll(['history'])
        self.ledger.reserve('123', self.now)
        self.ledger.receive('123', 'history', '456', exact_match=True)
        self.ledger.delivery('123', self.now, confirmed=True)
        self.assertEqual([], self.ledger.plan('123', self.now + timedelta(days=10)))

    def test_message_is_neutral_and_no_sensitive_reply(self):
        body = message(REQUIREMENTS)
        self.assertIn('Either choice is accepted', body)
        self.assertIn('do not email medical details', body)
        self.assertIn('do not email identity documents', body)
        self.assertNotIn('Approved', body)

    def test_duplicate_reserve_is_held(self):
        self.enroll()
        self.assertTrue(self.ledger.reserve('123', self.now))
        self.assertEqual([], self.ledger.reserve('123', self.now))

    def test_history_coverage_includes_explained_breaks(self):
        today = date(2026, 10, 12)
        self.assertEqual([], history_gaps([(date(2016, 10, 12), date(2020, 1, 1)),
            (date(2020, 1, 2), today)], today))
        self.assertEqual([(date(2020, 1, 2), date(2020, 1, 31))], history_gaps([
            (date(2016, 10, 12), date(2020, 1, 1)), (date(2020, 2, 1), today)], today))

    def test_unknown_dates_require_review(self):
        with self.assertRaises(ValueError):
            history_gaps([(None, date(2026, 10, 12))], date(2026, 10, 12))

    def test_dbs_completeness_only(self):
        flags = dict.fromkeys(DBS_DETAILS, True)
        flags['five_year_address_history'] = False
        self.assertEqual(['five_year_address_history'], missing_dbs_details(flags))
        with self.assertRaises(ValueError):
            missing_dbs_details({'name': 'actual personal data'})

    def test_html_form_links_are_clickable(self):
        body = html_message(['medical', 'working_time'])
        self.assertIn('<a href="https://form.jotform.com/90715470243352">Open secure form</a>', body)
        self.assertNotIn('https://form.jotform.com/211923425771051', body)

    def test_receipt_cannot_be_reused_for_another_candidate(self):
        self.enroll(['medical'])
        self.ledger.receive('123', 'medical', '456', exact_match=True)
        self.ledger.request('789', ['medical'], self.now, verified_link=True, staff_requested=True)
        with self.assertRaises(ValueError):
            self.ledger.receive('789', 'medical', '456', exact_match=True)
        self.assertEqual(['medical'], self.ledger.plan('789', self.now))


if __name__ == '__main__':
    unittest.main()
