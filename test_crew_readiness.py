import unittest
from crew_readiness import preview, format_review

class ReadinessTests(unittest.TestCase):
    def run_preview(self, **kw):
        return preview(['c','j','y'], ['c','j','y'], {'c':'B','j':'B','y':'D'},
                       '2026-10-13T16:44:00+01:00', source='verified pulse', **kw)
    def test_estimated_next_day(self):
        r=self.run_preview(comparable_hours=[18,16.5,16+50/60])
        self.assertEqual(r['name_suffix'],'(20:50 TBC)')
        self.assertTrue(r['split_team_review'])
        self.assertFalse(r['dispatch_ready'])
    def test_actual_replaces_estimate(self):
        r=self.run_preview(release='2026-10-14T09:00:00+01:00')
        self.assertEqual(r['name_suffix'],'(20:00)')
        self.assertFalse(r['provisional'])
    def test_twelve_hour_boundary(self):
        self.assertIsNone(self.run_preview(release='2026-10-14T04:44:00+01:00')['name_suffix'])
        self.assertEqual(self.run_preview(release='2026-10-14T04:45:00+01:00')['name_suffix'],'(15:45)')
    def test_day_off_excluded(self):
        r=preview(['c','j','y'],['c','j'],{'c':'B','j':'B','y':'D'},None)
        self.assertFalse(r['split_team_review'])
        self.assertNotIn('y',r['working_crew'])
    def test_no_estimate_without_comparables(self):
        self.assertIsNone(self.run_preview()['ready_from'])
    def test_bad_chronology(self):
        with self.assertRaises(ValueError): self.run_preview(release='2026-10-13T15:00:00+01:00')
    def test_readable_message(self):
        text=format_review(self.run_preview(comparable_hours=[17]),{'c':'Colin','j':'Jade','y':'Yeshayah'})
        self.assertIn('\n\n',text)
        self.assertIn('• Colin (20:44 TBC)',text)

if __name__=='__main__': unittest.main()
