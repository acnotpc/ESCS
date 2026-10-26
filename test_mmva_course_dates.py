import copy
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from mmva_course_dates import CourseDateWorker, Ecwid, build_router, plan_product, start_date


def course(choices=None):
    return {'id': 624650008, 'name': 'MMVA three-day course', 'price': 250, 'enabled': True,
            'options': [{'name': 'Date', 'type': 'SELECT', 'required': True, 'defaultChoice': None,
                         'choices': choices or [{'text': 'Monday 21 September 2026 to Wednesday 23 September 2026', 'priceModifier': 0},
                                                {'text': 'Monday 26 October 2026 to Wednesday 28 October 2026', 'priceModifier': 0},
                                                {'text': 'Monday 23 November 2026 to Wednesday 25 November 2026', 'priceModifier': 0}]},
                        {'name': 'I have completed training', 'type': 'RADIO', 'choices': [{'text': 'Yes'}, {'text': 'No'}]}]}


class FakeEcwid:
    def __init__(self):
        self.product = course()
        self.calls = []
        self.fail_read = False
        self.fail_after_write = False
        self.variants = []
        self.concurrent_change = False

    async def products(self):
        if self.fail_read:
            raise RuntimeError('secret-example-token-must-not-leak')
        return [copy.deepcopy(self.product)]

    async def request(self, path, method='GET', payload=None):
        self.calls.append((path, method, copy.deepcopy(payload)))
        if path.endswith('/combinations'):
            return self.variants
        if method == 'PUT':
            assert list(payload) == ['options']
            self.product['options'] = copy.deepcopy(payload['options'])
            if self.fail_after_write:
                raise RuntimeError('Uncertain provider response')
            return {'updateCount': 1}
        product = copy.deepcopy(self.product)
        if self.concurrent_change:
            product['options'][0]['required'] = False
        return product


class DateRules(unittest.TestCase):
    def test_label_formats(self):
        cases = {
            'Monday 23 November 2026 to Wednesday 25 November 2026': date(2026, 11, 23),
            'Quarter 1 - Wednesday 4th February to Friday 6th February 2026': date(2026, 2, 4),
            'Quarter 4 2026 – Monday 2 November to Friday 6 November 2026': date(2026, 11, 2),
            'Monday 28 December 2026 to Friday 1 January 2027': date(2026, 12, 28),
            'By arrangement': None, 'Monday 31 February 2027': None,
        }
        for text, expected in cases.items():
            self.assertEqual(start_date(text), expected)

    def test_minimal_change_and_default_remap(self):
        product = course()
        product['options'][0]['defaultChoice'] = 1
        before = copy.deepcopy(product)
        plan = plan_product(product, date(2026, 10, 26))
        self.assertEqual(len(plan['removals'][0]['removed']), 1)
        self.assertEqual(len(plan['options'][0]['choices']), 2)
        self.assertEqual(plan['options'][0]['defaultChoice'], 0)
        self.assertEqual(plan['options'][1], product['options'][1])
        self.assertEqual(product, before)
        product['options'][0]['defaultChoice'] = 0
        self.assertIsNone(plan_product(product, date(2026, 10, 26))['options'][0]['defaultChoice'])

    def test_ambiguous_and_all_expired_are_review_only(self):
        for choices in [[{'text': 'Monday 21 September 2026'}],
                        [{'text': 'Monday 21 September 2026'}, {'text': 'By arrangement'}]]:
            product = course(choices)
            plan = plan_product(product, date(2026, 10, 26))
            self.assertFalse(plan['changed'])
            self.assertTrue(plan['warnings'])
            self.assertEqual(plan['options'], product['options'])


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = {'MMVA_COURSE_DATES_ENABLED': 'true', 'MMVA_COURSE_DATES_WRITE_ENABLED': 'true',
                    'MMVA_COURSE_DATES_DB': str(Path(self.temp.name) / 'dates.sqlite3')}
        self.reader = FakeEcwid()
        self.clock = datetime(2026, 10, 26, 7, tzinfo=timezone.utc)
        self.worker = CourseDateWorker(self.env, self.reader, lambda: self.clock)

    async def asyncTearDown(self):
        self.worker.close()
        self.temp.cleanup()

    async def test_disabled_and_missing_token_have_no_io(self):
        worker = CourseDateWorker({}, now=lambda: self.clock)
        self.assertEqual((await worker.run())['state'], 'disabled')
        self.assertIsNone(worker.db)
        worker = CourseDateWorker({'MMVA_COURSE_DATES_ENABLED': 'true', 'MMVA_COURSE_DATES_WRITE_ENABLED': 'true'}, now=lambda: self.clock)
        self.assertEqual((await worker.run())['state'], 'not_connected')
        self.assertIsNone(worker.db)
        self.env['MMVA_COURSE_DATES_WRITE_ENABLED'] = 'false'
        self.assertEqual((await self.worker.run())['state'], 'disabled')
        self.assertFalse(self.reader.calls)

    async def test_preview_has_no_catalogue_writes(self):
        result = await self.worker.run(preview=True)
        self.assertEqual(result['state'], 'preview')
        self.assertEqual(len(result['updates']), 1)
        self.assertFalse(self.reader.calls)
        self.assertEqual(self.worker.ready().execute('SELECT count(*) FROM mmva_date_updates').fetchone()[0], 0)

    async def test_write_audit_readback_and_restart_idempotency(self):
        result = await self.worker.run(scheduled=True)
        self.assertEqual(result['changes'], 1)
        self.assertEqual(self.reader.product['price'], 250)
        self.assertTrue(self.reader.product['enabled'])
        self.assertEqual([x[2] for x in self.reader.calls if x[1] == 'PUT'], [{'options': self.reader.product['options']}])
        audit = self.worker.ready().execute('SELECT * FROM mmva_date_updates').fetchone()
        self.assertEqual(audit['status'], 'verified')
        self.assertEqual(len(json.loads(audit['before_options'])[0]['choices']), 3)
        self.assertEqual((await self.worker.run(scheduled=True))['state'], 'already_checked')
        self.worker.close()
        self.worker = CourseDateWorker(self.env, self.reader, lambda: self.clock)
        self.assertEqual((await self.worker.run(scheduled=True))['state'], 'already_checked')
        self.assertEqual(len([x for x in self.reader.calls if x[1] == 'PUT']), 1)

    async def test_uncertain_write_pauses_until_read_reconciliation(self):
        self.reader.fail_after_write = True
        result = await self.worker.run()
        self.assertEqual(result['state'], 'review_required')
        self.assertEqual((await self.worker.run())['state'], 'review_required')
        self.assertTrue(self.worker.status()['paused_for_review'])
        self.reader.fail_after_write = False
        result = await self.worker.reconcile()
        self.assertEqual(result['state'], 'reconciled')
        self.assertEqual(result['ecwid_writes'], 0)
        self.assertEqual(len([x for x in self.reader.calls if x[1] == 'PUT']), 1)

    async def test_source_failure_is_redacted_and_retried(self):
        self.reader.fail_read = True
        result = await self.worker.run(scheduled=True)
        self.assertEqual(result['state'], 'connection_or_schema_error')
        self.assertNotIn('secret-example', json.dumps(self.worker.status()))
        self.reader.fail_read = False
        self.assertEqual((await self.worker.run(scheduled=True))['changes'], 1)

    async def test_variants_and_concurrent_changes_prevent_writes(self):
        self.reader.variants = [{'id': 1}]
        result = await self.worker.run()
        self.assertTrue(result['warnings'])
        self.assertFalse([x for x in self.reader.calls if x[1] == 'PUT'])
        self.reader.variants = []
        self.reader.concurrent_change = True
        self.assertEqual((await self.worker.run())['state'], 'review_required')
        self.assertFalse([x for x in self.reader.calls if x[1] == 'PUT'])

    async def test_schedule_uses_uk_day(self):
        self.clock = datetime(2026, 7, 2, 4, 59, tzinfo=timezone.utc)
        self.assertEqual((await self.worker.run(scheduled=True))['state'], 'before_daily_window')
        self.assertIsNone(self.worker.db)
        self.clock = datetime(2026, 7, 2, 5, 0, tzinfo=timezone.utc)
        result = await self.worker.run(scheduled=True)
        self.assertEqual(result['day'], '2026-07-02')

    async def test_existing_lease_blocks_other_worker(self):
        self.worker.set_state('lease', json.dumps({'owner': 'other', 'expires': '2026-10-26T08:00:00+00:00'}))
        self.assertEqual((await self.worker.run())['state'], 'busy')
        self.assertFalse(self.reader.calls)


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_access_is_pinned_and_redacted(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(request.headers['Authorization'], 'Bearer token-for-test')
            self.assertTrue(request.url.path.startswith('/api/v3/76123907/products'))
            return httpx.Response(200, json={'items': [], 'total': 0})
        api = Ecwid('token-for-test', httpx.MockTransport(handler))
        self.assertEqual(await api.products(), [])
        for path in ['/orders', '/customers', '/products/1?secret=x']:
            with self.assertRaises(RuntimeError):
                await api.request(path)
        self.assertEqual(len(calls), 1)

    async def test_pagination_failure_never_returns_partial_catalogue(self):
        def handler(request):
            return httpx.Response(200, json={'items': [course()], 'total': 2})
        api = Ecwid('test', httpx.MockTransport(handler))
        with self.assertRaises(RuntimeError):
            await api.products()

    async def test_operator_endpoints_require_existing_connector_key(self):
        worker = CourseDateWorker({})
        def protection(x_connector_key: str | None = Header(default=None)):
            if x_connector_key != 'test-key':
                raise HTTPException(401, 'Unauthorized')
        app = FastAPI()
        app.include_router(build_router(worker, protection))
        with TestClient(app) as client:
            self.assertEqual(client.get('/mmva/course-dates/status').status_code, 401)
            self.assertEqual(client.post('/mmva/course-dates/run').status_code, 401)
            result = client.get('/mmva/course-dates/status', headers={'X-Connector-Key': 'test-key'})
            self.assertEqual(result.status_code, 200)
            self.assertFalse(result.json()['enabled'])
        self.assertIsNone(worker.db)
