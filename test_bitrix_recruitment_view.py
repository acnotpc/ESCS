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
