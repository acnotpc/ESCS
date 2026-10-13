import asyncio
import tempfile
import unittest
from fastapi import FastAPI
import httpx
from switchboard_bridge import Bridge, build_router, phone


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bridge = Bridge(self.tmp.name + '/calls.db', rest='https://escs.bitrix24.com/rest/1/test/', secret='test-only', enabled=True, write=True)
        self.methods = []
        self.matches = {}
        async def api(method, params):
            self.methods.append((method, params))
            if method == 'profile':
                return {'ID': 1}
            if method == 'crm.duplicate.findbycomm':
                return self.matches
            if method == 'crm.contact.company.items.get':
                return []
            return 123
        self.bridge.api = api

    def tearDown(self):
        if self.bridge.db:
            self.bridge.db.close()
        self.tmp.cleanup()

    def event(self, **fields):
        return dict(item='inbound_call', action='call_complete', ref='test-call-00001', callersNumber='07123456789', Result='Missed', **fields)

    def flush(self):
        asyncio.run(self.bridge.flush())

    def test_phone(self):
        self.assertEqual(phone('07123 456789'), '+447123456789')
        self.assertEqual(phone('00447123456789'), '+447123456789')
        self.assertIsNone(phone('Withheld'))

    def test_new_and_duplicate(self):
        self.bridge.receive(self.event())
        self.flush()
        self.bridge.receive(self.event())
        self.flush()
        self.assertEqual(sum(m == 'crm.contact.add' for m, p in self.methods), 1)
        self.assertEqual(sum(m == 'crm.timeline.comment.add' for m, p in self.methods), 1)
        self.assertEqual(self.bridge.status()['counts'], {'done': 1})

    def test_existing_company_no_fake_contact(self):
        self.matches = {'COMPANY': [456]}
        self.bridge.receive(self.event())
        self.flush()
        self.assertFalse(any(m == 'crm.contact.add' for m, p in self.methods))
        comment = next(p for m, p in self.methods if m == 'crm.timeline.comment.add')
        self.assertEqual(comment['fields']['ENTITY_TYPE'], 'company')

    def test_empty_list_match(self):
        self.matches = []
        self.bridge.receive(self.event())
        self.flush()
        self.assertEqual(self.bridge.status()['counts'], {'done': 1})

    def test_ambiguous_and_withheld(self):
        self.matches = {'CONTACT': [1, 2]}
        self.bridge.receive(self.event())
        self.flush()
        self.assertEqual(self.bridge.status()['counts'], {'review': 1})
        self.assertFalse(any(m.endswith('.add') for m, p in self.methods))

    def test_outbound_out_of_order(self):
        self.bridge.receive({'item':'outbound_call','action':'call_complete','ref':'outbound-test-01','result':'Answered'})
        self.flush()
        self.assertEqual(self.bridge.status()['counts'], {'pending': 1})
        self.bridge.receive({'item':'outbound_call','action':'call_start','ref':'outbound-test-01','numberDialled':'07123456789'})
        self.flush()
        self.assertEqual(self.bridge.status()['counts'], {'done': 1})

    def test_uncertain_write_does_not_retry(self):
        original = self.bridge.api
        async def fail(method, params):
            result = await original(method, params)
            if method == 'crm.contact.add':
                raise TimeoutError()
            return result
        self.bridge.api = fail
        self.bridge.receive(self.event())
        self.flush()
        self.flush()
        self.assertEqual(sum(m == 'crm.contact.add' for m, p in self.methods), 1)
        self.assertEqual(self.bridge.status()['counts'], {'review': 1})

    def test_preview_never_writes(self):
        self.bridge.write = False
        self.bridge.receive(self.event())
        self.flush()
        self.assertEqual(self.bridge.status()['counts'], {'preview': 1})
        self.assertFalse(any(m.endswith('.add') for m, p in self.methods))

    def test_http_auth_and_filtering(self):
        app = FastAPI()
        app.include_router(build_router(self.bridge, lambda: None))
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                self.assertEqual((await client.post('/switchboard/events', json=self.event())).status_code, 401)
                self.assertEqual((await client.get('/switchboard/delivery-status')).status_code, 401)
                self.assertEqual((await client.get('/switchboard/delivery-status', headers={'x-sbf-secret':'test-only'})).status_code, 200)
                self.assertEqual((await client.post('/switchboard/events/wrong', json=self.event())).status_code, 401)
                self.assertEqual((await client.get('/switchboard/delivery-status', headers={'x-sbf-secret':self.bridge.ingress_token()})).status_code, 401)
                event = self.event(transcript='must not store')
                response = await client.post('/switchboard/events', json=event, headers={'x-sbf-secret':'test-only'})
                self.assertEqual(response.status_code, 200)
                stored = self.bridge.db.execute('SELECT payload FROM calls').fetchone()[0]
                self.assertNotIn('transcript', stored)
                self.assertEqual((await client.post('/switchboard/events', json={'item':'billing'}, headers={'x-sbf-secret':'test-only'})).status_code, 400)
                self.assertEqual((await client.post('/switchboard/events/'+self.bridge.ingress_token(), json=self.event())).status_code, 200)
        asyncio.run(check())



if __name__ == '__main__':
    unittest.main()
