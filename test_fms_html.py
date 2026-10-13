from datetime import datetime, timezone, timedelta
import unittest

from fms_html import inline_colour, parse_html_export, read_html_cells, build_html_router, preview_html_rota
from test_fms_xlsx import STAFF
import test_fms_xlsx

NOW = datetime(2026, 10, 13, 19, 10, tzinfo=timezone.utc)
STAMP = NOW.isoformat()


def html_fixture(person_style="background-color:#0c0", group_style="background-color:#2d6c8c"):
    dates = [(NOW.date() - timedelta(days=1) + timedelta(days=i)).strftime("%d/%m/%Y") for i in range(7)]
    header = '<tr><td></td>' + ''.join(f'<td date="{d}"></td>' for d in dates) + '</tr>'
    rows = []
    for number in range(1, 5):
        entries = []
        for day in range(7):
            style = person_style if day == 3 else 'background-color:#f3c'
            entries.append(f'<td style="{group_style}"><div class="oU" style="{style}"><span>Person{number} Example (P)</span></div></td>')
        rows.append(f'<tr><td>Staff Group {number}</td>' + ''.join(entries) + '</tr>')
    rows.append('<tr><td>Office Support / meetings</td>' + ''.join(
        '<td><div class="oT">09:30-10:30</div><div class="oC">Meeting</div><div class="oU">Person1 Example</div></td>' if day == 3 else '<td></td>' for day in range(7)) + '</tr>')
    return ('<!DOCTYPE html><html><head><title>Export</title></head><body><table id="tblWeeklyView"><tbody>' + header + ''.join(rows) + '</tbody></table></body></html>').encode()


class HTMLTests(unittest.TestCase):
    def parse(self, **kwargs):
        return parse_html_export(html_fixture(**kwargs), STAFF, STAMP, NOW)

    def test_individual_holiday_overrides_blue_group(self):
        result = self.parse()
        day = result['days']['2026-10-15']
        self.assertTrue(result['individual_colours_verified'])
        self.assertEqual(day['staff'][0]['marker'], 'holiday')
        self.assertEqual(day['counts']['blue'], 0)
        self.assertEqual(day['counts_scope'], 'individual_colour_markers')
        self.assertTrue(day['staff'][0]['source_locator'].startswith('#tblWeeklyView'))
        self.assertEqual(day['staff'][0]['next_day_marker'], 'pink')
        self.assertFalse(result['dispatch_ready'])

    def test_individual_blue_overrides_pink_group(self):
        day = self.parse(person_style='background-color:#085278', group_style='background-color:#f3c')['days']['2026-10-15']
        self.assertEqual(day['counts']['blue'], 4)

    def test_individual_cyan_keeps_meeting_evidence(self):
        day = self.parse(person_style='background-color:#0ff')['days']['2026-10-15']
        self.assertEqual(day['staff'][0]['marker'], 'light_blue')
        self.assertEqual(day['staff'][0]['commitment_evidence'][0]['time_evidence']['text'], '09:30-10:30')

    def test_missing_or_unknown_person_style_never_inherits_group(self):
        for style in ['', 'color:#fff', 'background-color:#abcdef', 'background:url(x)']:
            result = self.parse(person_style=style)
            self.assertFalse(result['individual_colours_verified'])
            day = result['days']['2026-10-15']
            self.assertEqual(day['counts']['blue'], 0)
            self.assertEqual(day['staff'][0]['marker'], 'unknown')
            self.assertTrue(any(i['reason'] == 'individual_colour_verification_required' for i in day['issues']))

    def test_last_css_background_declaration_controls_marker(self):
        self.assertEqual(inline_colour('background-color:#085278;background-color:#0c0'), '00CC00')
        self.assertIsNone(inline_colour('background-color:#085278;background:url(x)'))

    def test_ambiguous_names_require_review(self):
        result = parse_html_export(html_fixture(), STAFF + [{**STAFF[0], 'staffId': 'duplicate'}], STAMP, NOW)
        self.assertFalse(result['individual_colours_verified'])
        self.assertTrue(any(i['reason'] == 'ambiguous_name' for i in result['days']['2026-10-15']['issues']))

    def test_inactive_record_not_available(self):
        staff = [dict(x) for x in STAFF]
        staff[0]['active'] = False
        day = parse_html_export(html_fixture(person_style='background-color:#085278'), staff, STAMP, NOW)['days']['2026-10-15']
        self.assertEqual(day['counts']['blue'], 3)

    def test_stale_html_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '24 hours'):
            parse_html_export(html_fixture(), STAFF, (NOW - timedelta(days=2)).isoformat(), NOW)

    def test_active_content_duplicate_tables_and_bad_layout_rejected(self):
        good = html_fixture()
        cases = [good.replace(b'</head>', b'<script>alert(1)</script></head>'),
                 good + good, good.replace(b'<td date=', b'<td colspan="2" date=', 1),
                 good.replace(b'</table>', b'', 1)]
        for raw in cases:
            with self.assertRaises(ValueError):
                read_html_cells(raw)

    def test_noncontinuous_dates_rejected(self):
        with self.assertRaisesRegex(ValueError, 'continuous'):
            parse_html_export(html_fixture().replace(b'15/10/2026', b'20/10/2026'), STAFF, STAMP, NOW)


class ConnectorPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        async def get(path, params):
            self.calls.append((path, params))
            return STAFF
        self.get = get

    async def preview(self, day='2026-10-15', html=None, stamp=STAMP):
        return await preview_html_rota(html or html_fixture().decode(), stamp, day, self.get, NOW)

    async def test_current_staff_and_requested_next_day_without_writes(self):
        r = await self.preview()
        self.assertEqual(self.calls, [('staff/list', {'teamId': '4h2o8q6h'})])
        self.assertEqual(list(r['days']), ['2026-10-15', '2026-10-16'])
        self.assertEqual(len(r['export_date_coverage']), 7)
        self.assertTrue(r['next_day_covered'])
        self.assertTrue(r['individual_colours_verified'])
        for key in ('saved_evidence', 'plans_changed', 'posts_changed', 'dispatch_ready'):
            self.assertFalse(r[key])

    async def test_final_day_reports_missing_next_week(self):
        r = await self.preview(day='2026-10-18')
        self.assertFalse(r['next_day_covered'])
        self.assertEqual(list(r['days']), ['2026-10-18'])

    async def test_oversize_malformed_and_bad_date_do_not_fetch_staff(self):
        for html, day in [('x' * (2 * 1024 * 1024 + 1), '2026-10-15'),
                          ('<script></script>', '2026-10-15'),
                          (html_fixture().decode(), '20261015')]:
            with self.assertRaises(ValueError):
                await self.preview(day=day, html=html)
        self.assertEqual(self.calls, [])

    async def test_stale_and_uncovered_service_date_rejected(self):
        with self.assertRaisesRegex(ValueError, '24 hours'):
            await self.preview(stamp=(NOW - timedelta(days=2)).isoformat())
        with self.assertRaisesRegex(ValueError, 'outside'):
            await self.preview(day='2026-10-19')

    async def test_upstream_failure_is_not_a_successful_preview(self):
        async def fail(path, params):
            raise RuntimeError('upstream unavailable')
        with self.assertRaisesRegex(RuntimeError, 'upstream unavailable'):
            await preview_html_rota(html_fixture().decode(), STAMP, '2026-10-15', fail, NOW)


class HTMLRouterTests(test_fms_xlsx.RouterTests):
    # Reuse meaningful auth, preview and storage invariants for the new route.
    def setUp(self):
        super().setUp()
        async def get(path, params):
            return STAFF
        # The inherited app's auth is private to setUp, so create a separate
        # authenticated client for the HTML endpoint below.
        from fastapi import FastAPI, Header, HTTPException
        from fastapi.testclient import TestClient
        def authorize(x_connector_key: str | None = Header(default=None)):
            if x_connector_key != 'test':
                raise HTTPException(401)
        app = FastAPI()
        app.include_router(build_html_router(self.daily, authorize, get, 'team'))
        self.daily.now = lambda: NOW
        self.client = TestClient(app)

    def upload(self, **data):
        return self.client.post('/team-sync/daily/html-import', headers={'X-Connector-Key': 'test'}, content=html_fixture(), params={'observed_at': STAMP, **data})

    def test_requires_existing_auth(self):
        self.assertEqual(self.client.post('/team-sync/daily/html-import').status_code, 401)

    def test_durable_idempotent_evidence_without_recapture(self):
        self.assertEqual(self.upload(save_evidence='true').status_code, 200)
        self.assertEqual(self.upload(save_evidence='true').status_code, 200)
        self.assertEqual(self.db.execute('SELECT count(*) FROM fms_colour_exports').fetchone()[0], 1)
        result = self.client.post('/team-sync/daily/html-import', headers={'X-Connector-Key': 'test'}, content=html_fixture(), params={'observed_at': (NOW - timedelta(minutes=30)).isoformat(), 'save_evidence': 'true'})
        self.assertEqual(result.status_code, 409)


if __name__ == '__main__':
    unittest.main()
