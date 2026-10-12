import copy
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import httpx
from reference_chasing import Ledger
from reference_monday_sync import (BOARD, SOURCE_COLUMN, SUMMARY_COLUMN, QUERIES,
                                   MondayReferenceSync, summary)
from test_reference_chasing import submission, NOW
from reference_chasing_service import ChasingService


class BoardSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(self.temp.name + '/sync.sqlite', 100)
        self.rows = self.ledger.enroll(submission(), NOW)
        self.calls, self.writes = [], []
        self.item = {'id': '200', 'name': 'Test', 'board': {'id': BOARD}, 'column_values': [
            {'id': SOURCE_COLUMN, 'text': '101'}, {'id': SUMMARY_COLUMN, 'text': ''},
            {'id': 'last_name', 'text': 'Candidate'}, {'id': 'status53', 'text': 'Pending Review'}]}
        self.matches, self.cursor = None, None
        self.read_hook, self.error, self.schema_type = None, False, 'long_text'
        async def source(path):
            self.assertEqual(path, '/submission/101')
            return copy.deepcopy(submission())
        self.jotform = SimpleNamespace(_get=source)

    def tearDown(self):
        self.ledger.db.close()
        self.temp.cleanup()

    def response(self, request):
        self.assertEqual(str(request.url), 'https://api.monday.com/v2')
        self.assertEqual(request.headers['API-Version'], '2026-10')
        payload = json.loads(request.content)
        operation = next(k for k, query in QUERIES.items() if query == payload['query'])
        self.calls.append(operation)
        if self.error:
            return httpx.Response(200, json={'errors': [{'message': 'secret provider detail'}]})
        if operation == 'schema':
            data = {'boards': [{'id': BOARD, 'columns': [
                {'id': SOURCE_COLUMN, 'type': 'text'}, {'id': SUMMARY_COLUMN, 'type': self.schema_type},
                {'id': 'last_name', 'type': 'text'}, {'id': 'status53', 'type': 'status'}]}]}
        elif operation == 'find':
            self.assertEqual(payload['variables'], {'source': '101'})
            data = {'items_page_by_column_values': {'cursor': self.cursor,
                'items': self.matches if self.matches is not None else [copy.deepcopy(self.item)]}}
        elif operation == 'read':
            self.assertEqual(payload['variables'], {'ids': ['200']})
            if self.read_hook:
                self.read_hook(self.item)
            data = {'items': [copy.deepcopy(self.item)]}
        else:
            values = json.loads(payload['variables']['values'])
            self.assertEqual(set(values), {SUMMARY_COLUMN})
            self.assertEqual(payload['variables']['item'], '200')
            self.writes.append(values)
            next(v for v in self.item['column_values'] if v['id'] == SUMMARY_COLUMN)['text'] = values[SUMMARY_COLUMN]['text']
            data = {'change_multiple_column_values': {'id': '200'}}
        return httpx.Response(200, json={'data': data})

    def adapter(self, write=False):
        return MondayReferenceSync('test-connection', write, httpx.MockTransport(self.response))

    async def test_preview_checks_exact_mapping_without_writing(self):
        result = await self.adapter().run(self.ledger, self.jotform)
        self.assertEqual(result['ready'], 1)
        self.assertEqual(self.writes, [])

    async def test_updates_only_automation_summary_and_replay_is_idempotent(self):
        adapter = self.adapter(True)
        first = await adapter.run(self.ledger, self.jotform)
        second = await adapter.run(self.ledger, self.jotform)
        self.assertEqual(first['updated'], 1)
        self.assertEqual(second['unchanged'], 1)
        self.assertEqual(len(self.writes), 1)
        self.assertNotIn('first@example.org', str(self.writes))
        self.assertEqual([r['sends'] for r in self.ledger.rows()], [0, 0])

    async def test_missing_link_is_held_without_creating_candidate(self):
        self.matches = []
        result = await self.adapter(True).run(self.ledger, self.jotform)
        self.assertEqual(result['held'], 1)
        self.assertEqual(self.writes, [])

    async def test_duplicate_source_links_are_held(self):
        self.matches = [copy.deepcopy(self.item), copy.deepcopy(self.item)]
        self.assertEqual((await self.adapter(True).run(self.ledger, self.jotform))['held'], 1)
        self.assertEqual(self.writes, [])

    async def test_incomplete_lookup_page_is_held(self):
        self.cursor = 'more-results'
        self.assertEqual((await self.adapter(True).run(self.ledger, self.jotform))['held'], 1)
        self.assertEqual(self.writes, [])

    async def test_wrong_board_is_held(self):
        self.item['board']['id'] = '999'
        self.assertEqual((await self.adapter(True).run(self.ledger, self.jotform))['held'], 1)
        self.assertEqual(self.writes, [])

    async def test_wrong_candidate_is_held(self):
        self.item['name'] = 'Different'
        self.assertEqual((await self.adapter(True).run(self.ledger, self.jotform))['held'], 1)
        self.assertEqual(self.writes, [])

    async def test_employed_candidate_is_not_updated(self):
        next(v for v in self.item['column_values'] if v['id'] == 'status53')['text'] = 'Employed with ESCS'
        self.assertEqual((await self.adapter(True).run(self.ledger, self.jotform))['held'], 1)
        self.assertEqual(self.writes, [])

    async def test_source_link_changed_before_write_is_held(self):
        def change(item):
            next(v for v in item['column_values'] if v['id'] == SOURCE_COLUMN)['text'] = '102'
        self.read_hook = change
        self.assertEqual((await self.adapter(True).run(self.ledger, self.jotform))['held'], 1)
        self.assertEqual(self.writes, [])

    async def test_schema_drift_blocks_every_write(self):
        self.schema_type = 'status'
        with self.assertRaises(RuntimeError):
            await self.adapter(True).run(self.ledger, self.jotform)
        self.assertEqual(self.writes, [])

    async def test_graphql_error_does_not_reflect_provider_details(self):
        self.error = True
        with self.assertRaises(RuntimeError) as caught:
            await self.adapter(True).run(self.ledger, self.jotform)
        self.assertNotIn('secret provider detail', str(caught.exception))
        self.assertEqual(self.writes, [])

    async def test_received_summary_remains_unverified(self):
        self.ledger.stop(self.rows[0]['id'], 'form', 'test-receipt')
        await self.adapter(True).run(self.ledger, self.jotform)
        text = self.writes[0][SUMMARY_COLUMN]['text']
        self.assertIn('1 of 2 received', text)
        self.assertIn('staff verification required', text)
        self.assertNotIn('All confirmed', text)

    async def test_invalid_source_identity_is_held(self):
        async def wrong(path):
            value = submission()
            value['form_id'] = '999'
            return value
        self.jotform._get = wrong
        self.assertEqual((await self.adapter(True).run(self.ledger, self.jotform))['held'], 1)
        self.assertEqual(self.writes, [])

    async def test_source_review_is_reported_without_changing_reference_state(self):
        self.ledger.review_source('101', NOW)
        await self.adapter(True).run(self.ledger, self.jotform)
        self.assertIn('Checklist details require staff review', self.writes[0][SUMMARY_COLUMN]['text'])
        self.assertTrue(all(r['state'] == 'source_review' for r in self.ledger.rows()))

    async def test_worker_board_sync_disabled_by_default_does_not_connect(self):
        service = ChasingService()
        with patch.dict('os.environ', {}, clear=True), patch('reference_chasing_service.MondayReferenceSync') as adapter:
            await service.sync_monday(None)
        adapter.assert_not_called()
        self.assertEqual(service.monday_sync, {'enabled': False, 'writeEnabled': False})

    async def test_board_failure_does_not_raise_or_alter_reference_requests(self):
        service = ChasingService()
        service.engine = SimpleNamespace(ledger=self.ledger)
        before = self.ledger.rows()
        fake = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError('secret token provider payload')))
        env = {'ESCS_REFERENCE_MONDAY_SYNC_ENABLED': 'true', 'MONDAY_RECRUITMENT_API_TOKEN': 'fake'}
        with patch.dict('os.environ', env, clear=True), patch('reference_chasing_service.MondayReferenceSync', return_value=fake):
            await service.sync_monday(self.jotform)
        self.assertIsNotNone(service.monday_sync['lastError'])
        self.assertNotIn('secret token', str(service.monday_sync))
        self.assertEqual(self.ledger.rows(), before)

    async def test_enabled_board_connection_starts_in_preview_mode(self):
        service = ChasingService()
        service.engine = SimpleNamespace(ledger=self.ledger)
        fake = SimpleNamespace(run=AsyncMock(return_value={'ready': 1, 'updated': 0}))
        env = {'ESCS_REFERENCE_MONDAY_SYNC_ENABLED': 'true', 'MONDAY_RECRUITMENT_API_TOKEN': 'fake'}
        with patch.dict('os.environ', env, clear=True), patch('reference_chasing_service.MondayReferenceSync', return_value=fake) as adapter:
            await service.sync_monday(self.jotform)
        adapter.assert_called_once_with('fake', False)
        self.assertEqual(service.monday_sync['updated'], 0)
        self.assertFalse(service.monday_sync['writeEnabled'])


if __name__ == '__main__':
    unittest.main()
