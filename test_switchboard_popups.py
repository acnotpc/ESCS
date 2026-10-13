import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException

from switchboard_bridge import Bridge
from switchboard_popups import CallActions, build_app_router


class PopupTests(unittest.TestCase):
    def setUp(self):
        self.envpatch=patch.dict(os.environ,{'SWITCHBOARD_APP_SESSION_SECRET':'independent-test-only-secret'})
        self.envpatch.start()
        self.tmp=tempfile.TemporaryDirectory()
        self.bridge=Bridge(self.tmp.name+'/bridge.db',rest='https://escs.bitrix24.com/rest/1/test/',secret='test-secret',enabled=True,write=True)
        self.service=CallActions(self.bridge)
        self.calls=[]
        async def crm(method,params):
            return {'CONTACT':[99]}
        self.bridge.api=crm
        self.users=[{'ID':'1037','NAME':'Control Room','LAST_NAME':'Team'},{'ID':'3509','NAME':'Accounts','LAST_NAME':'Team'},{'ID':'800','NAME':'Recruitment','LAST_NAME':'Administrator'}]
        async def api(method,params):
            self.calls.append((method,params))
            if method=='user.get':
                return self.users
            if method=='telephony.externalCall.register':
                return {'CALL_ID':'external.test'}
            if method=='crm.activity.todo.add':
                return {'id':123}
            return True
        self.service.api=api
        self.service.save_auth({'access_token':'fake-access','refresh_token':'fake-refresh','application_token':'fake-app','client_id':'local.test','expires_at':time.time()+3600})

    def tearDown(self):
        if self.bridge.db:
            self.bridge.db.close()
        self.tmp.cleanup()
        self.envpatch.stop()

    def event(self, option=None, action='call_start'):
        data={'item':'inbound_call','action':action,'ref':'popup-test-00001','calledNumber':'08435236264','callersNumber':'07123456789'}
        if option is not None:
            data['departmentNumber']=option
        with patch.dict(os.environ,{'SWITCHBOARD_POPUP_MENU_FIELD':'departmentNumber'}):
            self.bridge.receive(data)

    def test_disabled_does_not_pop(self):
        self.event(3)
        asyncio.run(self.service.flush())
        self.assertFalse(self.calls)

    def test_unknown_route_is_not_guessed(self):
        self.service.enabled=True
        self.event()
        asyncio.run(self.service.flush())
        self.assertFalse(any(m.startswith('telephony.') for m,p in self.calls))

    def test_verified_department_and_deduplication(self):
        self.service.enabled=True
        self.event(3)
        asyncio.run(self.service.flush())
        asyncio.run(self.service.flush())
        registrations=[p for m,p in self.calls if m=='telephony.externalCall.register']
        self.assertEqual(len(registrations),1)
        self.assertEqual(registrations[0]['USER_ID'],3509)
        self.assertEqual(registrations[0]['SHOW'],1)
        self.assertEqual(registrations[0]['CRM_CREATE'],0)

    def test_ambiguous_recipient_is_not_used(self):
        self.service.enabled=True
        self.users.append({'ID':'3510','NAME':'Accounts','LAST_NAME':'Team'})
        self.event(3)
        asyncio.run(self.service.flush())
        self.assertFalse(any(m.startswith('telephony.') for m,p in self.calls))

    def test_historical_calls_do_not_pop(self):
        self.service.enabled=True
        self.event(3)
        db=self.bridge.db
        row=db.execute('SELECT payload FROM calls').fetchone()
        data=json.loads(row[0]);data['_event_received_at']=time.time()-1000
        db.execute('UPDATE calls SET payload=?',(json.dumps(data),));db.commit()
        asyncio.run(self.service.flush())
        self.assertFalse(any(m.startswith('telephony.') for m,p in self.calls))

    def test_oauth_tokens_are_encrypted(self):
        raw=self.service.db().execute('SELECT value FROM sbf_app_auth').fetchone()[0]
        self.assertNotIn('fake-access',raw)
        self.assertEqual(self.service.auth()['access_token'],'fake-access')

    def test_installation_checks_portal_and_admin(self):
        async def raw(token,method,params):
            if method=='profile':
                return {'ID':1037}
            if method=='app.info':
                return {'CODE':'local.test'}
            return ['crm','telephony','placement','user_brief']
        self.service.raw_api=raw
        data={'auth':{'access_token':'new-access','refresh_token':'new-refresh','domain':'other.bitrix24.com','application_token':'new-app'}}
        with self.assertRaises(HTTPException) as domain:
            asyncio.run(self.service.install(data))
        self.assertEqual(domain.exception.status_code,400)
        data['auth']['domain']='escs.bitrix24.com'
        with self.assertRaises(HTTPException) as admin:
            asyncio.run(self.service.install(data))
        self.assertEqual(admin.exception.status_code,403)
        self.assertEqual(self.service.auth()['access_token'],'fake-access')

    def test_call_end_and_outcome_publish_once(self):
        self.service.enabled=True
        self.event(3)
        asyncio.run(self.service.flush())
        self.event(3,action='call_complete')
        self.bridge.db.execute("UPDATE calls SET state='done'");self.bridge.db.commit()
        asyncio.run(self.service.flush())
        fields={'caller_name':'Example','enquiry':'Invoice enquiry','outcome':'resolved'}
        asyncio.run(self.service.record_outcome(3509,'popup-test-00001',fields))
        asyncio.run(self.service.flush())
        asyncio.run(self.service.flush())
        self.assertEqual(sum(m=='telephony.externalCall.finish' for m,p in self.calls),1)
        self.assertEqual(sum(m=='crm.activity.todo.add' for m,p in self.calls),1)
        self.assertEqual(sum(m=='crm.timeline.comment.add' for m,p in self.calls),1)
        self.assertEqual(sum(m=='crm.activity.update' for m,p in self.calls),1)

    def add_action(self, uid=800, department=4):
        self.service.db().execute('INSERT INTO sbf_call_actions(ref,user_id,department,created) VALUES(?,?,?,?)',('hr-test-0001',uid,department,time.time()))
        self.service.db().commit()

    def test_hr_access_and_required_outcome(self):
        self.add_action()
        fields={'caller_name':'Example','enquiry':'Reference enquiry','outcome':'resolved'}
        with self.assertRaises(HTTPException) as wrong:
            asyncio.run(self.service.record_outcome(1037,'hr-test-0001',fields))
        self.assertEqual(wrong.exception.status_code,403)
        with self.assertRaises(HTTPException) as incomplete:
            asyncio.run(self.service.record_outcome(800,'hr-test-0001',{}))
        self.assertEqual(incomplete.exception.status_code,400)
        result=asyncio.run(self.service.record_outcome(800,'hr-test-0001',fields))
        self.assertEqual(result['state'],'resolved')

    def test_callback_remains_outstanding(self):
        self.add_action()
        fields={'caller_name':'Example','enquiry':'Reference enquiry','outcome':'callback','callback_at':'2099-01-01T10:00:00+00:00'}
        result=asyncio.run(self.service.record_outcome(800,'hr-test-0001',fields))
        self.assertEqual(result['state'],'followup')
        self.assertEqual(self.service.status()['outstanding'],1)

    def test_hr_summary_is_not_copied_to_general_crm(self):
        self.add_action()
        self.service.db().execute("UPDATE sbf_call_actions SET entity_type='contact',entity_id=99");self.service.db().commit()
        fields={'caller_name':'Example','enquiry':'Private HR enquiry','outcome':'resolved'}
        asyncio.run(self.service.record_outcome(800,'hr-test-0001',fields))
        asyncio.run(self.service.publish_outcomes())
        self.assertFalse(self.calls)
        self.assertEqual(self.service.db().execute('SELECT published FROM sbf_action_audit').fetchone()[0],1)

    def test_bad_session_and_install_request_rejected(self):
        with self.assertRaises(HTTPException):
            self.service.user_session('invalid')
        app=FastAPI();app.include_router(build_app_router(self.service))
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                response=await client.post('/switchboard/app/install/wrong',json={})
                self.assertEqual(response.status_code,401)
                response=await client.post('/switchboard/app/frame',data={'AUTH_ID':'fake'})
                self.assertEqual(response.status_code,401)
                response=await client.post('/switchboard/app/outcome',data={'session':'invalid'})
                self.assertEqual(response.status_code,401)
        asyncio.run(check())


if __name__=='__main__':
    unittest.main()
