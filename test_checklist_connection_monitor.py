import unittest
from checklist_connection_monitor import ConnectionMonitor


class Reader:
    def __init__(self, ready=True, failure=False):
        self.ready, self.failure = ready, failure
    async def inspect_schemas(self):
        if self.failure:
            raise RuntimeError('secret medical content must never be logged')
        return {k: {'signatureSchemaValid': True, 'trackingSchemaValid': self.ready,
                    'choiceSchemaValid': True} for k in ['history', 'medical', 'working_time']}


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_read_flags_required(self):
        m = ConnectionMonitor(Reader())
        with self.assertLogs('escs.checklist.connection', 'INFO') as logs:
            result = await m.check()
        self.assertTrue(result['connectionOK'])
        self.assertTrue(result['schemaReady'])
        self.assertFalse(result['candidateEmailsEnabled'])
        self.assertIn('forms=3 signatures=3 tracking=3', logs.output[0])

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
