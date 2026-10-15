import json
import os
import unittest
from unittest.mock import patch
from daily_teams import Candidate, MapProfile, Plan, proposed_teams, proximity_order


class BaseProximityTests(unittest.TestCase):
    def members(self):
        return [Candidate(staff_id=str(i), name=name, active=True, marker='blue',
                          regular_group=1, map_profile=MapProfile(order=i+1, area='verified map'))
                for i, name in enumerate(['A', 'B', 'C', 'D', 'E', 'F'])]

    def locations(self):
        return {name.lower(): (lat, 0) for name, lat in
                [('A', 50), ('B', 55), ('C', 50.01), ('D', 55.01), ('E', 50.02), ('F', 55.02)]}

    def test_nearby_staff_replace_seniority_chunks(self):
        ordered = proximity_order(self.members(), self.locations())
        crews = [{c.name for c in ordered[i:i+3]} for i in (0,3)]
        self.assertEqual(crews, [{'A','C','E'}, {'B','D','F'}])

    def test_four_remain_two_pairs(self):
        ordered = proximity_order(self.members()[:4], self.locations())
        self.assertEqual([{c.name for c in ordered[i:i+2]} for i in (0,2)], [{'A','C'}, {'B','D'}])

    def test_integrated_group_boundaries_and_rota_exclusions(self):
        people = self.members()
        people[4].marker = 'pink'
        people[5].regular_group = 2
        plan = Plan(service_date='2026-10-16', observed_at='2026-10-15T07:00:00+01:00',
                    source_reference='FMS', roster_complete=True, map_reference='map-test',
                    map_checked_at='2026-10-15T07:00:00+01:00', candidates=people)
        config = {'map_reference':'map-test','observed_at':plan.observed_at,
                  'staff':[{'name':n,'latitude':v[0],'longitude':v[1]} for n,v in self.locations().items()]}
        with patch.dict(os.environ, {'BITRIX_DAILY_TEAMS_BASE_MAP':json.dumps(config)}):
            rows = proposed_teams(plan)
            self.assertEqual(rows, ['Team A - A, C', 'Team B - B, D', 'Team C - F'])
            config['staff'].pop()
        with patch.dict(os.environ, {'BITRIX_DAILY_TEAMS_BASE_MAP':json.dumps(config)}):
            self.assertIn('requires verification', proposed_teams(plan)[0])

    def test_invalid_or_stale_evidence_rejected(self):
        from daily_teams import base_locations
        plan = Plan(service_date='2026-10-16', observed_at='2026-10-15T07:00:00+01:00',
                    source_reference='FMS', roster_complete=True, map_reference='map-test', candidates=[])
        for config in [
            {'map_reference':'wrong','observed_at':plan.observed_at,'staff':[]},
            {'map_reference':'map-test','observed_at':'2026-01-01T00:00:00Z','staff':[]},
            {'map_reference':'map-test','observed_at':plan.observed_at,'staff':[{'name':'A','latitude':91,'longitude':0}]},
        ]:
            with patch.dict(os.environ, {'BITRIX_DAILY_TEAMS_BASE_MAP':json.dumps(config)}):
                with self.assertRaises(ValueError): base_locations(plan)
