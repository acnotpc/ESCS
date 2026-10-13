import unittest
from crew_readiness import planning_checks

class PlanningTests(unittest.TestCase):
    def check(self, staff, teams=None, now='2026-10-13T22:49:00+01:00'):
        return planning_checks('2026-10-13',staff,teams or {},now=now)
    def fresh(self, id='new'):
        return dict(id=id,today_colour='pink',tomorrow_colour='blue',tomorrow_ready_from='2026-10-14T06:00:00+01:00')
    def test_mix_flags_potential_not_confirmed_commitment(self):
        r=self.check([self.fresh(),dict(id='old',today_colour='blue',tomorrow_colour='blue')],{'A':['new','old']})
        self.assertEqual(len(r['mixed_team_reviews']),1)
        self.assertEqual(r['potential_overnight_staff'],['old'])
        self.assertEqual(r['allocation_changes'],[])
    def test_fresh_team_kept_together(self):
        r=self.check([self.fresh('a'),self.fresh('b')],{'A':['a','b']})
        self.assertEqual(r['mixed_team_reviews'],[])
    def test_finish_boundary_and_estimate(self):
        staff=[dict(id='a',today_colour='blue',tomorrow_colour='pink',expected_release='2026-10-14T06:00:00+01:00'),dict(id='b',today_colour='blue',tomorrow_colour='pink',expected_release='2026-10-14T06:01:00+01:00')]
        r=self.check(staff)
        self.assertEqual([x['id'] for x in r['finish_target_reviews']],['b'])
        self.assertTrue(r['finish_target_reviews'][0]['estimated'])
    def test_unknown_release_still_needs_plan(self):
        r=self.check([dict(id='a',today_colour='blue',tomorrow_colour='pink')])
        self.assertIn('unknown',r['finish_target_reviews'][0]['reason'])
    def test_actual_overrides_estimate(self):
        r=self.check([dict(id='a',today_colour='blue',tomorrow_colour='pink',expected_release='2026-10-14T07:00:00+01:00',actual_release='2026-10-13T22:00:00+01:00')])
        self.assertEqual(r['finish_target_reviews'],[])
    def test_unverified_and_wrong_ready_time_not_fresh(self):
        s=self.fresh();s['tomorrow_ready_from']='2026-10-14T07:00:00+01:00'
        r=self.check([s,dict(id='unknown')])
        self.assertEqual(r['fresh_starters'],[])
        self.assertEqual(r['unknown_colour_staff'],['unknown'])
    def test_duplicate_allocation_rejected(self):
        with self.assertRaises(ValueError):self.check([self.fresh()],{'A':['new'],'B':['new']})
    def test_future_actual_rejected(self):
        with self.assertRaises(ValueError):self.check([dict(id='a',today_colour='blue',tomorrow_colour='pink',actual_release='2026-10-14T07:00:00+01:00')])
    def test_dst_cutoff_uses_local_day(self):
        r=planning_checks('2026-10-24',[],{},now='2026-10-24T22:00:00+01:00')
        self.assertEqual(r['finish_target'],'2026-10-25T06:00:00+00:00')

if __name__=='__main__':unittest.main()
