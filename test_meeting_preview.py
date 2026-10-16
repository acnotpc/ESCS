import unittest
from copy import deepcopy
from meeting_preview import meeting_preview
from daily_teams import Candidate, Plan, Interval


class MeetingTests(unittest.TestCase):
    def setUp(self):
        self.plan = Plan(service_date='2026-10-16', observed_at='2026-10-16T06:00:00+01:00',
                         source_reference='test FMS', roster_complete=True,
                         previous_day_date='2026-10-15', previous_day_observed_at='2026-10-16T06:00:00+01:00',
                         previous_day_source_reference='verified previous-day FMS',
                         candidates=[Candidate(staff_id='a', name='A', active=True, marker='blue', previous_day_marker='pink',
                            availability=Interval(start='2026-10-16T06:00:00+01:00', end='2026-10-16T18:00:00+01:00'),
                            commitments_checked=True, duties_checked=True)])
        self.kw = dict(point={'id':'base','label':'Verified test base','verified':True},
                       route={'origin_id':'base','destination_id':'d1','mode':'road','verified':True,
                              'source':'operator reviewed road route','observed_at':'2026-10-16T06:00:00+01:00',
                              'valid_from':'2026-10-16T06:00:00+01:00','valid_until':'2026-10-16T12:00:00+01:00',
                              'duration_minutes':60}, d1_id='d1', required_at='2026-10-16T09:00:00+01:00',
                       now='2026-10-16T06:00:00+01:00')

    def test_checks_and_early_arrival(self):
        r = meeting_preview(self.plan,['a'],**self.kw)
        self.assertEqual(r['meet_at'],'2026-10-16T07:30:00+01:00')
        self.assertEqual(r['depart_at'],'2026-10-16T07:45:00+01:00')
        self.assertEqual(r['d1_arrival'],'2026-10-16T08:45:00+01:00')
        self.assertFalse(r['dispatch_ready'])
        self.assertEqual(r['reviews'],[])

    def test_bad_route_evidence(self):
        for field,value in [('mode','straight_line'),('destination_id','other'),('verified',False),
                            ('duration_minutes',float('nan')),('duration_minutes',True),
                            ('observed_at','2026-10-14T06:00:00+01:00'),
                            ('valid_until','2026-10-16T07:00:00+01:00')]:
            kw=deepcopy(self.kw); kw['route'][field]=value
            with self.subTest(field=field,value=value), self.assertRaises(ValueError):
                meeting_preview(self.plan,['a'],**kw)

    def test_pink_or_unknown_never_cleared(self):
        for colour in ('pink','unknown','holiday','sick','training'):
            self.plan.candidates[0].marker=colour
            r=meeting_preview(self.plan,['a'],**self.kw)
            self.assertTrue(r['reviews']); self.assertFalse(r['dispatch_ready'])

    def test_missing_release_or_commitment_blocks(self):
        self.plan.candidates[0].commitments_checked=False
        self.assertTrue(meeting_preview(self.plan,['a'],**self.kw)['reviews'])

    def test_cross_midnight_uses_dates(self):
        self.kw['required_at']='2026-10-16T00:30:00+01:00'
        self.kw['route']['valid_from']='2026-10-15T00:00:00+01:00'
        r=meeting_preview(self.plan,['a'],**self.kw)
        self.assertEqual(r['meet_at'],'2026-10-15T23:00:00+01:00')
        self.assertTrue(r['reviews'])

class MeetingEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_preview_does_not_save_or_publish(self):
        from types import SimpleNamespace
        from datetime import datetime
        from fastapi import FastAPI, Header, HTTPException
        import httpx
        from daily_teams import build_daily_router
        fixture=MeetingTests(); fixture.setUp()
        daily=SimpleNamespace(now=lambda: datetime.fromisoformat(fixture.kw['now']))
        def auth(x_connector_key: str | None = Header(None)):
            if x_connector_key != 'test-key':
                raise HTTPException(401,'Unauthorized')
        app=FastAPI(); app.include_router(build_daily_router(daily,auth))
        body={k:v for k,v in fixture.kw.items() if k != 'now'}
        body.update(plan=fixture.plan.model_dump(),staff_ids=['a'])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            r=await client.post('/team-sync/daily/meeting-preview',json=body)
            self.assertEqual(r.status_code,401)
            r=await client.post('/team-sync/daily/meeting-preview',json=body,headers={'x-connector-key':'test-key'})
            self.assertEqual(r.status_code,200,r.text)
            self.assertFalse(r.json()['posts_changed'])
            self.assertFalse(r.json()['plans_changed'])
            self.assertFalse(r.json()['meeting_review']['dispatch_ready'])
            body['route']['source']=''
            r=await client.post('/team-sync/daily/meeting-preview',json=body,headers={'x-connector-key':'test-key'})
            self.assertEqual(r.status_code,422)
