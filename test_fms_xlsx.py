import asyncio
from datetime import datetime, timezone
from io import BytesIO
import sqlite3
from types import SimpleNamespace
import unittest
from zipfile import ZipFile

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from fms_xlsx import parse_export, build_xlsx_router

NOW = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)
STAMP = NOW.isoformat()
STAFF = [{"staffId": str(i), "firstName": f"Person{i}", "lastName": "Example",
          "active": True, "dateStarted": "2025-01-01", "dateFinished": None}
         for i in range(1, 5)]


def fixture(colour="FFFF33CC", duplicate=False, conditional=False):
    dates = ["Mon. Oct. 05, 2026", "Tue. Oct. 06, 2026", "Wed. Oct. 07, 2026",
             "Thu. Oct. 08, 2026", "Fri. Oct. 09, 2026", "Sat. Oct. 10, 2026", "Sun. Oct. 11, 2026"]
    def cell(ref, text, style=0):
        return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t>{text}</t></is></c>'
    headers = ''.join(cell(f'{c}1', d) for c, d in zip('BCDEFGH', dates))
    rows = [f'<row r="1">{headers}</row>']
    for i in range(1, 5):
        cells = cell(f'A{i+1}', f'Staff Group {i}')
        for c in 'BCDEFGH':
            cells += cell(f'{c}{i+1}', f'Person{1 if duplicate else i} Example (P)', 1 if c == 'E' else 2)
        rows.append(f'<row r="{i+1}">{cells}</row>')
    rows += ['<row r="6">' + cell('A6', 'Office Support / meetings') + cell('E6', '09:30-10:30') + '</row>',
             '<row r="7">' + cell('E7', 'Meeting') + '</row>',
             '<row r="8">' + cell('E8', 'Person1 Example') + '</row>']
    xml = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + ''.join(rows) + '</sheetData>'
    xml += '<conditionalFormatting sqref="E2" />' if conditional else ''
    xml += '</worksheet>'
    out = BytesIO()
    with ZipFile(out, 'w') as z:
        z.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Rota" sheetId="1" r:id="rId1" /></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" /></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml', xml)
        z.writestr('xl/styles.xml', f'<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fills><fill><patternFill patternType="none" /></fill><fill><patternFill patternType="solid"><fgColor rgb="{colour}" /></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FF2D6C8C" /></patternFill></fill></fills><cellXfs><xf fillId="0"/><xf fillId="1"/><xf fillId="2"/></cellXfs></styleSheet>')
    return out.getvalue()


class ExportTests(unittest.TestCase):
    def parse(self, **kwargs):
        return parse_export(fixture(**kwargs), STAFF, STAMP, NOW)

    def test_exact_colours_and_next_day(self):
        data = self.parse()
        self.assertEqual(data['days']['2026-10-08']['staff'][0]['marker'], 'pink')
        self.assertEqual(data['days']['2026-10-07']['staff'][0]['next_day_marker'], 'pink')
        self.assertEqual(data['days']['2026-10-08']['staff'][0]['source_cell'], 'E2')
        self.assertFalse(data['dispatch_ready'])

    def test_cyan_with_candidate_meeting(self):
        data = self.parse(colour='FF00FFFF')['days']['2026-10-08']
        person = data['staff'][0]
        self.assertEqual(person['marker'], 'light_blue')
        self.assertEqual(person['commitment_evidence'][0]['time_evidence']['text'], '09:30-10:30')
        self.assertTrue(any(x['reason'] == 'light_blue_commitment_missing' for x in data['issues']))

    def test_white_and_unknown_are_not_available(self):
        for colour in ['FFFFFFFF', 'FFAABBCC']:
            result = self.parse(colour=colour)['days']['2026-10-08']
            self.assertEqual(result['counts']['blue'], 0)
            self.assertEqual(result['staff'][0]['marker'], 'unknown')
            self.assertEqual(result['issues'][0]['reason'], 'availability_marker_unconfirmed')

    def test_inactive_and_future_employee_excluded(self):
        staff = [dict(x) for x in STAFF]
        staff[0]['active'] = False
        staff[1]['dateStarted'] = '2027-01-01'
        data = parse_export(fixture(colour='FF2D6C8C'), staff, STAMP, NOW)
        self.assertEqual(data['days']['2026-10-08']['counts']['blue'], 2)

    def test_ambiguous_names_never_choose_first(self):
        staff = STAFF + [{**STAFF[0], 'staffId': 'other'}]
        data = parse_export(fixture(), staff, STAMP, NOW)
        self.assertEqual(data['days']['2026-10-08']['issues'][0]['reason'], 'ambiguous_name')

    def test_historical_duplicate_does_not_hide_active_staff(self):
        staff = STAFF + [{**STAFF[0], 'staffId': 'old', 'active': False, 'dateFinished': '2024-01-01'}]
        data = parse_export(fixture(), staff, STAMP, NOW)
        self.assertEqual(data['days']['2026-10-08']['staff'][0]['staff_id'], '1')

    def test_missing_group_member_is_reported_without_being_added(self):
        staff = STAFF + [{'staffId': 'outside', 'firstName': 'Other', 'lastName': 'Employee', 'active': True, 'department': 'Conveyance'}]
        data = parse_export(fixture(), staff, STAMP, NOW)['days']['2026-10-08']
        self.assertEqual(len(data['staff']), 4)
        self.assertEqual(data['active_conveyance_outside_groups'][0]['staff_id'], 'outside')

    def test_duplicate_group_members_block(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate staff'):
            self.parse(duplicate=True)

    def test_conditional_colours_block(self):
        with self.assertRaisesRegex(ValueError, 'Conditional'):
            self.parse(conditional=True)

    def test_stale_capture_not_refreshed_on_upload(self):
        with self.assertRaisesRegex(ValueError, '24 hours'):
            parse_export(fixture(), STAFF, '2026-10-05T15:00:00+00:00', NOW)
        with self.assertRaisesRegex(ValueError, 'Timezone'):
            parse_export(fixture(), STAFF, '2026-10-07T15:00:00', NOW)

    def test_invalid_zip_rejected(self):
        with self.assertRaises(ValueError):
            parse_export(b'not a zip', STAFF, STAMP, NOW)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.daily = SimpleNamespace(now=lambda: NOW, sync=SimpleNamespace(lock=asyncio.Lock()),
                                     ready=lambda: SimpleNamespace(db=self.db))
        def auth(x_connector_key: str | None = Header(default=None)):
            if x_connector_key != 'test':
                raise HTTPException(401)
        async def get(path, params):
            self.assertEqual((path, params), ('staff/list', {'teamId': 'team'}))
            return STAFF
        app = FastAPI()
        app.include_router(build_xlsx_router(self.daily, auth, get, 'team'))
        self.client = TestClient(app)

    def tearDown(self):
        self.db.close()

    def upload(self, **data):
        return self.client.post('/team-sync/daily/xlsx-import', headers={'X-Connector-Key': 'test'},
                                content=fixture(), params={'observed_at': STAMP, **data})

    def test_requires_existing_auth(self):
        self.assertEqual(self.client.post('/team-sync/daily/xlsx-import').status_code, 401)

    def test_preview_changes_neither_plans_nor_posts(self):
        result = self.upload()
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()['saved_evidence'])
        self.assertFalse(result.json()['plans_changed'])
        self.assertFalse(result.json()['posts_changed'])
        self.assertEqual(self.db.execute("SELECT count(*) FROM sqlite_master WHERE name='fms_colour_exports'").fetchone()[0], 0)

    def test_durable_idempotent_evidence_without_recapture(self):
        self.assertEqual(self.upload(save_evidence='true').status_code, 200)
        self.assertEqual(self.upload(save_evidence='true').status_code, 200)
        self.assertEqual(self.db.execute('SELECT count(*) FROM fms_colour_exports').fetchone()[0], 1)
        result = self.client.post('/team-sync/daily/xlsx-import', headers={'X-Connector-Key': 'test'},
                                 content=fixture(), params={'observed_at': '2026-10-07T14:30:00+00:00', 'save_evidence': 'true'})
        self.assertEqual(result.status_code, 409)


if __name__ == '__main__':
    unittest.main()
