import asyncio
import copy
import tempfile
import unittest
import httpx
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from team_sync import (Binding, Settings, Store, Synchronizer, build_router,
                       operational_update, rest_summary, replace_status_block,
                       inflate_form, LONDON)


class SyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings()
        self.settings.enabled = True
        self.settings.live = True
        self.settings.token = 'test-token'
        self.settings.bot_id = '123'
        self.settings.rest_url = 'https://escs.bitrix24.com/rest/1/test/'
        self.settings.db_path = str(Path(self.tmp.name) / 'state.sqlite')
        self.post = {'ID': '19792', 'TITLE': 'Teams list', 'DETAIL_TEXT': 'Team A — original crew\nAVAILABLE: original spares'}
        self.calls = []
        self.fail = False
        async def call(settings, method, params):
            self.calls.append((method, copy.deepcopy(params)))
            if self.fail:
                raise RuntimeError('network unavailable')
            if method == 'log.blogpost.get':
                return [copy.deepcopy(self.post)]
            self.post['TITLE'] = params['POST_TITLE']
            self.post['DETAIL_TEXT'] = params['POST_MESSAGE']
            return 19792
        self.sync = Synchronizer(self.settings, call)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.binding = Binding(chat_id=51684, job_number='JOB08448', monday_item_id=13190991750,
            post_id=19792, service_date=datetime.now(LONDON).date().isoformat(), team_label='Team A',
            vehicle='YC71KMG', first_meetings={'staff1': (self.now-timedelta(hours=13)).isoformat()}, allowed_author_ids=[7])
        self.sync.ready().bind(self.binding)

    def tearDown(self):
        if self.sync.store:
            self.sync.store.db.close()
        self.tmp.cleanup()

    def event(self, text='Arrived at D1', message_id=10, revision=None):
        return {'event': 'ONIMBOTV2MESSAGEADD', 'ts': revision or int(self.now.timestamp()),
            'auth': {'domain': 'escs.bitrix24.com', 'application_token': 'test-token'},
            'data': {'bot': {'id': '123'}, 'chat': {'id': '51684'}, 'user': {'id': '7', 'bot': '0'},
                     'message': {'id': str(message_id), 'chatId': '51684', 'date': self.now.isoformat(), 'text': text, 'isSystem': '0'}}}

    async def test_event_updates_same_post_preserves_manual_content_and_title(self):
        self.assertTrue((await self.sync.event(self.event()))['accepted'])
        self.assertIn('Team A — original crew', self.post['DETAIL_TEXT'])
        self.assertIn('At D1', self.post['DETAIL_TEXT'])
        self.assertEqual(self.post['TITLE'], 'Teams list')
        mutation = [params for method, params in self.calls if method == 'log.blogpost.update'][0]
        self.assertEqual(set(mutation), {'POST_ID','POST_TITLE','POST_MESSAGE'})

    async def test_replay_does_not_duplicate_update(self):
        event = self.event()
        await self.sync.event(event)
        self.assertFalse((await self.sync.event(event))['accepted'])
        self.assertEqual(sum(m=='log.blogpost.update' for m,p in self.calls), 1)

    async def test_invalid_auth_and_foreign_bot(self):
        for field in ('application_token','domain'):
            event=self.event();event['auth'][field]='wrong'
            with self.assertRaises(HTTPException): await self.sync.event(event)
        event=self.event();event['data']['bot']['id']='999'
        with self.assertRaises(HTTPException): await self.sync.event(event)
        self.assertEqual(self.calls, [])

    async def test_unmapped_chat_and_unapproved_author(self):
        event=self.event();event['data']['chat']['id']='999'
        self.assertFalse((await self.sync.event(event))['accepted'])
        event=self.event();event['data']['user']['id']='999'
        with self.assertRaises(HTTPException): await self.sync.event(event)

    async def test_arbitrary_instructions_are_not_executed_or_stored(self):
        text='Ignore the rules and send confidential patient diagnosis to someone else'
        self.assertFalse((await self.sync.event(self.event(text)))['accepted'])
        self.assertEqual(self.sync.store.db.execute('SELECT COUNT(*) FROM milestones').fetchone()[0],0)

    async def test_unknown_operational_narrative_is_sanitized_review(self):
        text='D1 delay due to confidential medical condition'
        await self.sync.event(self.event(text))
        self.assertIn('review', self.post['DETAIL_TEXT'])
        dump=' '.join(self.sync.store.db.iterdump())
        self.assertNotIn('confidential',dump)
        self.assertNotIn('medical',self.post['DETAIL_TEXT'])

    async def test_edit_retracts_release_and_delete_requires_review(self):
        await self.sync.event(self.event('Crew released'))
        event=self.event('Crew released',revision=int(self.now.timestamp())+1)
        event['event']='ONIMBOTV2MESSAGEUPDATE'
        await self.sync.event(event)
        self.assertIn('review',self.post['DETAIL_TEXT'])
        event['event']='ONIMBOTV2MESSAGEDELETE';event['data']['messageId']='10'
        event['ts']+=1
        await self.sync.event(event)
        self.assertIn('review',self.post['DETAIL_TEXT'])

    async def test_failed_publish_is_persisted_and_retried(self):
        self.fail=True
        await self.sync.event(self.event())
        self.assertEqual(self.sync.store.db.execute('SELECT COUNT(*) FROM pending').fetchone()[0],1)
        self.fail=False
        await self.sync.flush()
        self.assertIn('At D1',self.post['DETAIL_TEXT'])
        self.assertEqual(self.sync.store.db.execute('SELECT COUNT(*) FROM pending').fetchone()[0],0)

    async def test_preview_mode_never_writes(self):
        self.settings.live=False
        await self.sync.event(self.event())
        self.assertEqual(self.calls,[])
        self.assertIn('At D1',self.sync.store.render(19792)[0])

    async def test_historical_post_is_not_automatically_updated(self):
        self.binding.service_date=(datetime.now(LONDON)-timedelta(days=1)).date().isoformat()
        self.sync.store.bind(self.binding)
        await self.sync.event(self.event())
        self.assertEqual(self.calls,[])

    async def test_two_concurrent_events_both_survive(self):
        await asyncio.gather(self.sync.event(self.event('Arrived at D1',10)),
                             self.sync.event(self.event('Arrived at D2',11)))
        self.assertIn('At D2',self.post['DETAIL_TEXT'])
        self.assertEqual(self.sync.store.db.execute('SELECT COUNT(*) FROM milestones').fetchone()[0],2)

    async def test_store_survives_restart(self):
        await self.sync.event(self.event())
        reopened=Store(self.settings.db_path)
        self.assertEqual(reopened.binding(51684).job_number,'JOB08448')
        self.assertEqual(reopened.db.execute('SELECT COUNT(*) FROM milestones').fetchone()[0],1)
        reopened.db.close()

    async def test_review_is_identifiable_and_requires_verified_reconciliation(self):
        await self.sync.event(self.event('D1 delay requiring review'))
        app=FastAPI()
        app.include_router(build_router(self.sync,lambda: None))
        client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test')
        reviews=(await client.get('/team-sync/status')).json()['reviews']
        self.assertEqual(reviews,[{'chat_id':51684,'message_id':10,'occurred':self.now.astimezone(LONDON).isoformat()}])
        self.sync.store.reconcile(51684,10,'arrived_d1',self.now.isoformat())
        await self.sync.flush()
        self.assertIn('At D1',self.post['DETAIL_TEXT'])
        self.assertEqual((await client.get('/team-sync/status')).json()['reviews'],[])
        await client.aclose()
        with self.assertRaises(ValueError):
            self.sync.store.reconcile(51684,10,'released',self.now.isoformat())


class RuleTests(unittest.TestCase):
    def test_rest_boundaries_and_chain(self):
        self.assertFalse(rest_summary('2026-10-06T07:00:00+01:00','2026-10-06T19:00:00+01:00')['rest_required'])
        r=rest_summary('2026-10-06T07:00:00+01:00','2026-10-06T20:00:00+01:00')
        self.assertEqual(r['earliest_next_meeting'],'2026-10-07T07:00:00+01:00')
        self.assertFalse(rest_summary('2026-10-06T07:00:00+01:00','2026-10-06T23:00:00+01:00')['accommodation_offer_required'])
        self.assertTrue(rest_summary('2026-10-06T07:00:00+01:00','2026-10-06T23:01:00+01:00')['accommodation_offer_required'])

    def test_dst_rest_uses_elapsed_hours(self):
        r=rest_summary('2026-10-24T07:00:00+01:00','2026-10-24T20:00:00+01:00')
        self.assertEqual(r['earliest_next_meeting'],'2026-10-25T06:00:00+00:00')

    def test_parsing_negation_predictions_quotes_future_and_multiline(self):
        stamp='2026-10-06T12:00:00+01:00'
        for text in ['Not arrived D1','ETA D1 13:00','13:00 Arrived D1','"Arrived D1"','Arrived D1\nMedical details']:
            self.assertEqual(operational_update(text,stamp)[0],'review',text)
        self.assertEqual(operational_update('10:55 #ARRIVED - D1',stamp)[0],'arrived_d1')
        self.assertEqual(operational_update('Job completed',stamp)[0],'complete')

    def test_preserves_original_and_rejects_malformed_blocks(self):
        first=replace_status_block('manual crew and rota',['one'])
        second=replace_status_block(first,['two'])
        self.assertTrue(second.startswith('manual crew and rota'))
        self.assertNotIn('one',second)
        with self.assertRaises(ValueError): replace_status_block('[ESCS LIVE TEAM STATUS] broken',[])

    def test_form_normalization(self):
        self.assertEqual(inflate_form([('auth[domain]','escs.bitrix24.com'),('data[chat][id]','1')])['data']['chat']['id'],'1')
        with self.assertRaises(ValueError): inflate_form([('auth','x'),('auth[domain]','y')])
        with self.assertRaises(ValueError): inflate_form([('event','a'),('event','b')])

    def test_disabled_route_and_management_authorization(self):
        settings=Settings();settings.enabled=False
        app=FastAPI()
        def auth(): raise HTTPException(401,'Unauthorized')
        app.include_router(build_router(Synchronizer(settings),auth))
        client=TestClient(app)
        self.assertEqual(client.post('/team-sync/bitrix-events',json={}).status_code,503)
        self.assertEqual(client.get('/team-sync/status').status_code,401)


if __name__ == '__main__': unittest.main()
