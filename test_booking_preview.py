import unittest
from unittest.mock import AsyncMock
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException
from booking_preview import build_planning_router, preview_rota_with_planning
from test_fms_html import html_fixture, STAMP, NOW
from test_fms_xlsx import STAFF


class PlanningPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_colours_and_planning_share_existing_preview(self):
        fetch = AsyncMock(return_value=STAFF)
        sid = STAFF[0]['staffId']
        result = await preview_rota_with_planning(html_fixture().decode(), STAMP,
            '2026-10-14', '{"teams":{"A":["'+sid+'"]}}', fetch, NOW)
        self.assertIn('planning_review', result)
        self.assertFalse(result['posts_changed'])
        self.assertFalse(result['plans_changed'])
        self.assertFalse(result['saved_evidence'])
        self.assertFalse(result['planning_review']['dispatch_ready'])
        self.assertEqual(result['planning_review']['allocation_changes'], [])

    async def test_old_callers_keep_plain_preview(self):
        result = await preview_rota_with_planning(html_fixture().decode(), STAMP,
            '2026-10-14', '', AsyncMock(return_value=STAFF), NOW)
        self.assertNotIn('planning_review', result)

    async def test_invalid_plan_rejected_before_upstream(self):
        for plan in ('[]', 'invalid', '{"teams":[]}', '{"crews":{}}', ' '*100001):
            fetch = AsyncMock()
            with self.assertRaises(ValueError):
                await preview_rota_with_planning('', STAMP, '2026-10-14', plan, fetch, NOW)
            fetch.assert_not_awaited()

    async def test_route_requires_existing_auth_and_sanitizes_bad_input(self):
        fetch = AsyncMock(return_value=STAFF)
        def auth(x_connector_key: str | None = Header(None)):
            if x_connector_key != 'test-key':
                raise HTTPException(401, 'Unauthorized')
        app = FastAPI()
        app.include_router(build_planning_router(auth, fetch))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            r = await client.post('/team-sync/daily/planning-preview', json={})
            self.assertEqual(r.status_code, 401)
            fetch.assert_not_awaited()
            r = await client.post('/team-sync/daily/planning-preview',
                content='private-bad-input', headers={'x-connector-key':'test-key'})
            self.assertEqual(r.status_code, 422)
            self.assertNotIn('private-bad-input', r.text)

    async def test_authenticated_route_integration(self):
        # Move fixture's week and capture together so freshness validation is real.
        raw = html_fixture().decode()
        from datetime import timedelta
        current = datetime.now(timezone.utc)
        delta = current.date() - NOW.date()
        import re
        dates = {}
        for i in range(7):
            day = NOW.date() - timedelta(days=1) + timedelta(days=i)
            dates[day.strftime('%d/%m/%Y')] = (day+delta).strftime('%d/%m/%Y')
        raw = re.sub(r'\d{2}/\d{2}/\d{4}', lambda m: dates[m[0]], raw)
        app = FastAPI()
        app.include_router(build_planning_router(lambda: None, AsyncMock(return_value=STAFF)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            r = await client.post('/team-sync/daily/planning-preview', json={
                'html':raw, 'observed_at':current.isoformat(),
                'service_date':current.date().isoformat(), 'planning':{}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('planning_review', r.json())
        self.assertFalse(r.json()['posts_changed'])
