import os,sqlite3,unittest
from types import SimpleNamespace
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from bitrix_recruitment_view import build_bitrix_recruitment_view
class Tests(unittest.TestCase):
 def setUp(self):
  self.db=sqlite3.connect(':memory:',check_same_thread=False);self.db.row_factory=sqlite3.Row
  self.db.executescript("CREATE TABLE checklist_review(id TEXT,reason TEXT);CREATE TABLE checklist_candidate(id TEXT,state TEXT,sends INTEGER);INSERT INTO checklist_review VALUES('123','private health text');INSERT INTO checklist_candidate VALUES('123','hold',1);")
  self.owner='1';self.appid='106';self.calls=0
  def provider(req):
   self.calls+=1
   return httpx.Response(200,json={'result':{'ID':self.appid if 'app.info' in str(req.url) else self.owner}})
  app=FastAPI();app.include_router(build_bitrix_recruitment_view(SimpleNamespace(ledger=SimpleNamespace(db=self.db)),httpx.MockTransport(provider)))
  self.client=TestClient(app);os.environ['BITRIX_RECRUITMENT_VIEW_ENABLED']='true'
 def tearDown(self):self.db.close();os.environ.pop('BITRIX_RECRUITMENT_VIEW_ENABLED',None)
 def test_owner_live_read(self):
  r=self.client.post('/bitrix/recruitment/reviews',data={'AUTH_ID':'mock'});self.assertEqual(r.status_code,200)
  self.assertEqual(r.json()['reviews'][0]['itemId'],'123');self.assertNotIn('private health',r.text);self.assertTrue(r.json()['readOnly'])
 def test_other_user_denied(self):
  self.owner='2';self.assertEqual(self.client.post('/bitrix/recruitment/reviews',data={'AUTH_ID':'mock'}).status_code,403)
 def test_other_app_denied(self):
  self.appid='107';self.assertEqual(self.client.post('/bitrix/recruitment/reviews',data={'AUTH_ID':'mock'}).status_code,403)
 def test_missing_token_denied(self):
  self.assertEqual(self.client.post('/bitrix/recruitment/reviews').status_code,403);self.assertEqual(self.calls,0)
 def test_disabled(self):
  os.environ['BITRIX_RECRUITMENT_VIEW_ENABLED']='false';self.assertEqual(self.client.post('/bitrix/recruitment/reviews',data={'AUTH_ID':'mock'}).status_code,503)
 def test_no_get(self):self.assertEqual(self.client.get('/bitrix/recruitment/reviews').status_code,405)
if __name__=='__main__':unittest.main()

class SourceTests(unittest.IsolatedAsyncioTestCase):
 async def test_register_paginates_and_shows_missing_without_private_answers(self):
  from unittest.mock import patch
  from bitrix_recruitment_view import applicant_register
  from checklist_workflow import REQUEST,RECEIVED
  row=lambda id:{'id':id,'name':'Example','state':'active','group':{'title':'Applicants'},'column_values':[{'id':'status53','text':'Awaiting DBS'},{'id':REQUEST,'text':'10-year history','value':'{"ids":[1]}'},{'id':RECEIVED,'text':'','value':'{"ids":[]}'}]}
  calls=[]
  async def call(self,q,variables=None):
   calls.append(q)
   if len(calls)==1:return {'boards':[{'id':'424237712','name':'Staff recruitment hub','items_page':{'cursor':'next','items':[row('1')]}}]}
   return {'next_items_page':{'cursor':None,'items':[row('2')]}}
  with patch('checklist_workflow.MondayFollowup.call',call):
   result=await applicant_register(None)
  self.assertEqual(result['count'],2);self.assertTrue(result['complete']);self.assertEqual(result['items'][0]['outstanding'],['10-year history'])
  self.assertTrue(all('mutation' not in q for q in calls));self.assertNotIn('email',str(result['items'][0]))
 async def test_wrong_board_fails(self):
  from unittest.mock import patch,AsyncMock
  from bitrix_recruitment_view import applicant_register
  with patch('checklist_workflow.MondayFollowup.call',AsyncMock(return_value={'boards':[{'id':'999'}]})):
   with self.assertRaises(RuntimeError):await applicant_register(None)
 async def test_independent_source_failure(self):
  from unittest.mock import patch,AsyncMock
  from bitrix_recruitment_view import live_sources
  with patch('bitrix_recruitment_view.applicant_register',AsyncMock(side_effect=RuntimeError())),patch('bitrix_recruitment_view.form_connections',AsyncMock(return_value={'status':'connected'})),patch('bitrix_recruitment_view.mailbox_connections',AsyncMock(return_value=[])):
   result=await live_sources(None)
  self.assertEqual(result['register']['status'],'unavailable');self.assertEqual(result['jotform']['status'],'connected');self.assertFalse(result['fullyReconciled'])
 async def test_mailbox_not_exact_never_reads_messages(self):
  from unittest.mock import patch,AsyncMock
  from bitrix_recruitment_view import mailbox_connections
  with patch.dict(os.environ,{'BITRIX_RECRUITMENT_MAIL_REST_URL':'https://escs.bitrix24.com/rest/1/test-only/'}),patch('hr_mail_readiness.HRMailReadiness.call',AsyncMock(return_value={'items':[{'id':1,'email':'other@example.com'}]})),patch('recruitment_mail.RecruitmentMail._call',AsyncMock()) as msg:
   result=await mailbox_connections()
  self.assertTrue(all(r['status']=='connection_required' for r in result));msg.assert_not_awaited()
