import unittest
from unittest.mock import patch
import httpx
from checklist_connection_monitor import ConnectionMonitor
from recruitment_checklist_access import FORMS


class Reader:
    def __init__(self, ready=True, failure=False):
        self.ready, self.failure = ready, failure
    async def inspect_schemas(self):
        if self.failure:
            raise RuntimeError('secret medical content must never be logged')
        return {k: {'signatureSchemaValid': True, 'trackingSchemaValid': self.ready,
                    'choiceSchemaValid': True} for k in FORMS}


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_read_flags_required(self):
        m = ConnectionMonitor(Reader())
        with self.assertLogs('escs.checklist.connection', 'INFO') as logs:
            result = await m.check()
        self.assertTrue(result['connectionOK'])
        self.assertTrue(result['schemaReady'])
        self.assertFalse(result['candidateEmailsEnabled'])
        self.assertIn(f'forms={len(FORMS)} signatures={len(FORMS)} tracking={len(FORMS)}', logs.output[0])

    async def test_missing_tracking_not_ready(self):
        m = ConnectionMonitor(Reader(ready=False))
        result = await m.check()
        self.assertTrue(result['connectionOK'])
        self.assertFalse(result['schemaReady'])

    async def test_failure_redacted_and_last_success_retained(self):
        reader = Reader()
        m = ConnectionMonitor(reader)
        before = await m.check()
        reader.failure = True
        with self.assertLogs('escs.checklist.connection', 'INFO') as logs:
            result = await m.check()
        self.assertFalse(result['connectionOK'])
        self.assertEqual(before['lastSuccess'], result['lastSuccess'])
        self.assertNotIn('secret medical content', ''.join(logs.output))

    async def test_repeat_monitor_does_not_send(self):
        m = ConnectionMonitor(Reader())
        await m.check()
        again = await m.check()
        self.assertFalse(again['candidateEmailsEnabled'])

    async def test_monday_pinned_read_and_verified_identity(self):
        def handler(request):
            self.assertEqual('api.monday.com', request.url.host)
            self.assertNotIn('mutation', request.content.decode())
            self.assertIn('424237712', request.content.decode())
            return httpx.Response(200, json={'data': {'boards': [{'id': '424237712',
                'columns': [{'id': 'text_mm82vbx9', 'type': 'text'},
                            {'id': 'long_text_mm8220ks', 'type': 'long_text'}]}]}})
        with patch.dict('os.environ', {'MONDAY_RECRUITMENT_API_TOKEN': 'test-only'}):
            result = await ConnectionMonitor(Reader(), httpx.MockTransport(handler)).check()
        self.assertTrue(result['mondayConnectionOK'])
        self.assertFalse(result['candidateEmailsEnabled'])

    async def test_monday_bad_token_does_not_block_source_and_is_redacted(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(403, json={'errors': [{'message': 'secret-test-only'}]}))
        with patch.dict('os.environ', {'MONDAY_RECRUITMENT_API_TOKEN': 'test-only'}):
            with self.assertLogs('escs.checklist.connection', 'INFO') as logs:
                result = await ConnectionMonitor(Reader(), transport).check()
        self.assertTrue(result['connectionOK'])
        self.assertFalse(result['mondayConnectionOK'])
        self.assertNotIn('secret-test-only', ''.join(logs.output))

    async def test_monday_wrong_board_does_not_verify(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json={'data': {'boards': [{'id': '999', 'columns': []}]}}))
        with patch.dict('os.environ', {'MONDAY_RECRUITMENT_API_TOKEN': 'test-only'}):
            result = await ConnectionMonitor(Reader(), transport).check()
        self.assertFalse(result['mondayConnectionOK'])
