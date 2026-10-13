import unittest
from rota_planning import review_html_plan

class IntegrationTests(unittest.TestCase):
    def rota(self):
        return {'next_day_covered':True,'days':{'2026-10-13':{'staff':[{'staff_id':'a','marker':'pink','active':True,'individual_colour_verified':True}]},'2026-10-14':{'staff':[{'staff_id':'a','marker':'blue','active':True,'individual_colour_verified':True}]}}}
    def test_colours_from_html_and_readiness_from_operator(self):
        r=review_html_plan(self.rota(),'2026-10-13',{'staff':{'a':{'tomorrow_ready_from':'2026-10-14T06:00:00+01:00'}},'teams':{'A':['a']}},'2026-10-13T22:00:00+01:00')
        self.assertEqual(r['fresh_starters'],['a'])
    def test_unverified_colour_not_used(self):
        rota=self.rota();rota['days']['2026-10-13']['staff'][0]['individual_colour_verified']=False
        self.assertEqual(review_html_plan(rota,'2026-10-13',{},'2026-10-13T22:00:00+01:00')['fresh_starters'],[])
    def test_missing_next_day_rejected(self):
        r=self.rota();r['next_day_covered']=False
        with self.assertRaises(ValueError):review_html_plan(r,'2026-10-13',{},'2026-10-13T22:00:00+01:00')
    def test_unknown_id_rejected(self):
        with self.assertRaises(ValueError):review_html_plan(self.rota(),'2026-10-13',{'staff':{'bad':{}}},'2026-10-13T22:00:00+01:00')
