import unittest,tempfile
from datetime import datetime,timezone
from team_sync import Binding,Store,timing_update,replace_timing_fields,LONDON

class TimingTests(unittest.TestCase):
    def test_only_destination_times(self):
        sent='2026-10-07T05:21:00+01:00'
        self.assertEqual(timing_update('Departing D1 for D2, ETA 06:06',sent)[0], 'eta_d2')
        self.assertEqual(timing_update('Departing office. ETA to D1 05:10','2026-10-07T03:31:00+01:00')[0], 'eta_d1')
        self.assertEqual(timing_update('Arrived at D2','2026-10-07T06:07:00+01:00')[0], 'arrived_d2')
        for text in ['SU now going to Robin ward','Just waiting for paperwork to be checked at D2','Job complete','Not arrived at D2','Arrived D2\nPatient report']:
            self.assertEqual(timing_update(text,sent)[0], 'review')

    def test_inline_replaces_latest_by_message_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(tmp+'/s.db')
            day=datetime.now(LONDON).date().isoformat()
            b=Binding(chat_id=1,job_number='JOB08462',monday_item_id=10,post_id=20,service_date=day,team_label='Team B',vehicle='YC71KKX',first_meetings={},allowed_author_ids=[1037],timing_only=True).checked()
            store.bind(b)
            store.accept(b,1,1,'eta_d2',day+'T09:00:00+01:00')
            store.accept(b,2,2,'arrived_d2',day+'T08:50:00+01:00')
            before='Team A - Other\n\nTeam B - Jade, Daniel\n08462 - 05:45 - Route | Arrived D2 06:07 | YC71 KKX\n\nCommitments / hours\nPreserve me\n'
            after=replace_timing_fields(before,store,20)
            self.assertIn('Arrived D2 08:50 | YC71 KKX',after)
            self.assertNotIn('ETA D2 09:00',after)
            self.assertIn('Preserve me',after)
            self.assertEqual(replace_timing_fields(after,store,20),after)
            with self.assertRaises(ValueError):replace_timing_fields(before.replace('YC71 KKX','YC71 KMG'),store,20)
            with self.assertRaises(ValueError):replace_timing_fields(before.replace('Team B -','Team C -'),store,20)
            with self.assertRaises(ValueError):replace_timing_fields(before+before,store,20)

if __name__=='__main__':unittest.main()
