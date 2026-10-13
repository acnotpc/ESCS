import sqlite3
import unittest
from conveyance_time_reminders import Evidence, Job, ReminderLedger, evaluate

NOW = '2026-10-13T21:41:00+01:00'


class ReminderTests(unittest.TestCase):
    def test_future_eta_and_empty_cells_never_become_due(self):
        j = Job(1,'JOB08483',('a',),{},[Evidence('arrived_d2','2026-10-13T22:35:00+01:00','teams list',True)])
        self.assertEqual(evaluate([j],NOW)[0]['missing'],[])

    def test_actual_stage_missing_and_legacy_d2_alias(self):
        j=Job(1,'JOB08481',('a',),{'dup__of_hour':'07:00 PM'},[
            Evidence('arrived_d2','2026-10-13T19:00:00+01:00','board'),
            Evidence('end','2026-10-13T20:57:00+01:00','operator final return')])
        self.assertEqual([m['stage'] for m in evaluate([j],NOW)[0]['missing']],['end'])

    def test_b2b_three_jobs_preserve_origin_and_handover(self):
        one=Job(1,'JOB08480',('a','b'),{},[Evidence('start','2026-10-13T11:51:00+01:00','board')],handover_at='2026-10-13T13:00:00+01:00')
        two=Job(2,'JOB08481',('b','a'),{},[],1,True,'2026-10-13T15:05:00+01:00')
        three=Job(3,'JOB08483',('a','b'),{},[],2,True)
        r=evaluate([one,two,three],NOW)[2]
        self.assertEqual(r['continuous_duty_start'],'2026-10-13T10:51:00+00:00')
        self.assertEqual(r['review'],[])
        self.assertEqual(r['missing'][0]['stage'],'start')
        self.assertFalse(r['dispatch_ready'])

    def test_crew_change_and_unverified_handover_require_review(self):
        one=Job(1,'JOB08480',('a',),{},[],handover_at='2026-10-13T13:00:00+01:00')
        two=Job(2,'JOB08481',('b',),{},[],1,True)
        self.assertIn('B2B handover/date/crew requires verification',evaluate([one,two],NOW)[1]['review'])
        two.crew_ids=('a',); two.handover_verified=False
        self.assertIsNone(evaluate([one,two],NOW)[1]['continuous_duty_start'])

    def test_conflicting_handover_does_not_replace_duty_origin(self):
        one=Job(1,'JOB08480',('a',),{},[Evidence('start','2026-10-13T11:51:00+01:00','board')],handover_at='2026-10-13T13:00:00+01:00')
        two=Job(2,'JOB08481',('a',),{},[Evidence('start','2026-10-13T14:00:00+01:00','board')],1,True)
        self.assertIn('Previous job end differs from next job start',evaluate([one,two],NOW)[1]['review'])

    def test_missing_original_start_and_cycle_are_not_inferred(self):
        one=Job(1,'JOB08480',('a',),{},[],handover_at='2026-10-13T13:00:00+01:00')
        two=Job(2,'JOB08481',('a',),{},[],1,True,'2026-10-13T15:00:00+01:00')
        self.assertIsNone(evaluate([one,two],NOW)[1]['continuous_duty_start'])
        one.previous_item_id=2; one.handover_verified=True
        with self.assertRaisesRegex(ValueError,'Cyclic'): evaluate([one,two],NOW)

    def test_restart_uncertain_send_and_changed_gaps_do_not_resend(self):
        db=sqlite3.connect(':memory:'); ledger=ReminderLedger(db)
        findings=[{'item_id':1,'missing':[{'stage':'end'}],'review':[]}]
        key=ledger.reserve('trial-20261013',findings)
        self.assertIsNotNone(key)
        self.assertIsNone(ReminderLedger(db).reserve('trial-20261013',findings))
        findings[0]['missing'].append({'stage':'arrived_d2'})
        self.assertIsNone(ledger.reserve('trial-20261013',findings))
        ledger.confirm_sent(key,'2026-10-13T21:38:13+01:00',2303738)
        self.assertEqual(ledger.elapsed(key,'2026-10-13T21:43:13+01:00'),300)
        with self.assertRaises(ValueError): ledger.elapsed(key,'2026-10-13T21:37:00+01:00')
        db.close()

    def test_naive_dates_and_duplicate_bookings_rejected(self):
        j=Job(1,'JOB08481',('a',),{},[Evidence('end','2026-10-13T20:57:00','board')])
        with self.assertRaises(ValueError): evaluate([j],NOW)
        with self.assertRaises(ValueError): evaluate([j,j],NOW)


if __name__=='__main__': unittest.main()
