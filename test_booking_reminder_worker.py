import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from booking_reminder_worker import ReminderWorker, FIELDS, MondayReader
from team_sync import Store, Binding

NOW=datetime(2026,10,15,8,tzinfo=timezone.utc)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name+'/state.db')
        self.settings=SimpleNamespace(validate=lambda:None,rest_url='https://escs.bitrix24.com/rest/1/existing/')
        self.messages=[]
        async def call(settings,method,params):
            if method=='im.recent.list':return [{'id':'1037','type':'user','title':'Control Room Team'}]
            if method=='im.message.add':
                self.messages.append({'id':101,'author_id':1,'date':NOW.isoformat(),'text':params['MESSAGE']})
                return 101
            if method=='im.dialog.messages.get':return {'messages':self.messages if not params.get('LAST_ID') else []}
            raise AssertionError(method)
        self.sync=SimpleNamespace(ready=lambda:self.store,call=AsyncMock(side_effect=call),settings=self.settings)
        self.cells={c:None for names in FIELDS.values() for c in names};self.cells['text1']='JOB08485'
        self.reader=SimpleNamespace(read=AsyncMock(return_value={1:self.cells}))
        self.worker=ReminderWorker(self.sync,self.reader,now=lambda:NOW)
        self.env=patch.dict('os.environ',{'CONVEYANCE_REMINDERS_ENABLED':'true','CONVEYANCE_REMINDERS_WRITE_ENABLED':'true'})
        self.env.start()
        binding=Binding(chat_id=5,job_number='JOB08485',monday_item_id=1,post_id=10,
            service_date='2026-10-15',team_label='Team A',vehicle='NA25YHH',
            first_meetings={'a':'2026-10-15T06:00:00+01:00'},allowed_author_ids=[1])
        self.store.bind(binding)
        self.store.accept(binding,20,1,'arrived_d1',(NOW-timedelta(hours=1)).isoformat())

    def tearDown(self):
        self.env.stop();self.store.db.close();self.tmp.cleanup()

    async def test_send_readback_and_restart_dedup(self):
        r=await self.worker.run();self.assertEqual(r['state'],'sent')
        self.assertIn('JOB08485: Arrive D1',self.messages[0]['text'])
        restarted=ReminderWorker(self.sync,self.reader,now=lambda:NOW)
        self.assertEqual((await restarted.run())['state'],'nothing_due')
        self.assertEqual(len(self.messages),1)

    async def test_preview_never_sends_or_reserves(self):
        r=await self.worker.run(preview=True)
        self.assertEqual(r['reminder_jobs'],1);self.assertEqual(self.messages,[])
        self.assertEqual(self.worker.db().execute('SELECT count(*) FROM booking_reminder_runs').fetchone()[0],0)

    async def test_populated_legacy_arrival_is_not_chased(self):
        self.cells['date1']='2026-10-15 08:00'
        self.assertEqual((await self.worker.run())['state'],'nothing_due')
        self.assertEqual(self.messages,[])

    async def test_quiet_hours_and_hourly_bound(self):
        self.worker.now=lambda:NOW.replace(hour=1)
        self.assertEqual((await self.worker.run())['state'],'disabled_or_quiet_hours')
        self.reader.read.assert_not_awaited()
        self.worker.now=lambda:NOW
        await self.worker.run()
        self.assertEqual((await self.worker.run())['state'],'hour_already_checked')

    async def test_ack_is_separate_from_board_completion(self):
        await self.worker.run()
        self.messages.append({'id':102,'author_id':1037,'date':(NOW+timedelta(minutes=1)).isoformat(),
            'text':'ACK ESCS-REM-20261015-09'})
        self.worker.now=lambda:NOW+timedelta(hours=1)
        self.assertEqual((await self.worker.run())['state'],'nothing_due')
        self.assertIsNotNone(self.worker.db().execute('SELECT acknowledged_at FROM booking_reminder_runs').fetchone()[0])
        self.assertIsNone(self.cells['hour_mm6w9kt5'])

    async def test_wrong_author_cannot_acknowledge(self):
        await self.worker.run()
        self.messages.append({'id':102,'author_id':99,'date':NOW.isoformat(),'text':'ACK ESCS-REM-20261015-09'})
        self.worker.now=lambda:NOW+timedelta(hours=1)
        await self.worker.run()
        self.assertIsNone(self.worker.db().execute('SELECT acknowledged_at FROM booking_reminder_runs').fetchone()[0])

    async def test_uncertain_send_survives_restart_and_blocks(self):
        original=self.sync.call
        async def call(settings,method,params):
            if method=='im.message.add':raise RuntimeError('timeout')
            return await original(settings,method,params)
        self.sync.call=AsyncMock(side_effect=call)
        with self.assertRaises(RuntimeError):await self.worker.run()
        self.sync.call=original
        with self.assertRaises(RuntimeError):await ReminderWorker(self.sync,self.reader,now=lambda:NOW).run()
        self.assertEqual(self.messages,[])

    async def test_failed_readback_blocks_resend(self):
        original=self.sync.call
        async def call(settings,method,params):
            if method=='im.dialog.messages.get':return {'messages':[]}
            return await original(settings,method,params)
        self.sync.call=AsyncMock(side_effect=call)
        with self.assertRaises(RuntimeError):await self.worker.run()
        self.assertEqual(self.worker.db().execute('SELECT state FROM booking_reminder_runs').fetchone()[0],'sending')

    async def test_identity_or_missing_column_blocks(self):
        self.cells['text1']='JOB00000'
        with self.assertRaises(RuntimeError):await self.worker.run(preview=True)
        self.cells['text1']='JOB08485';self.cells.pop('hour9')
        with self.assertRaises(RuntimeError):await self.worker.run(preview=True)
        self.assertEqual(self.messages,[])

    async def test_review_and_recent_events_are_not_chased(self):
        self.store.db.execute("UPDATE milestones SET status='review'");self.store.db.commit()
        self.assertEqual((await self.worker.run(preview=True))['reminder_jobs'],0)
        self.store.db.execute("UPDATE milestones SET status='arrived_d1',occurred=?",(NOW.isoformat(),));self.store.db.commit()
        self.assertEqual((await self.worker.run(preview=True))['reminder_jobs'],0)

    async def test_different_crew_on_same_booking_requires_review(self):
        b=Binding.model_validate_json(self.store.db.execute('SELECT data FROM bindings').fetchone()[0])
        b.chat_id=6;b.first_meetings={'b':NOW.isoformat()};self.store.bind(b)
        self.store.accept(b,21,1,'arrived_d1',(NOW-timedelta(hours=1)).isoformat())
        with self.assertRaises(RuntimeError):await self.worker.run(preview=True)

    async def test_conflicting_actuals_not_sent(self):
        b=self.store.binding(5)
        self.store.accept(b,21,1,'arrived_d1',(NOW-timedelta(hours=2)).isoformat())
        self.assertEqual((await self.worker.run())['state'],'nothing_due')


class MondayReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_connection_does_not_call_network(self):
        with patch.dict('os.environ',{},clear=True):
            with self.assertRaisesRegex(RuntimeError,'connection unavailable'):
                await MondayReader().read([1])
