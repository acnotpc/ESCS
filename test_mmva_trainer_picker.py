import asyncio
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from mmva_attendance import build_attendance_router
from mmva_monday_courses import MondayCourses, BOARD
from test_mmva_monday_courses import item, CODE, reader

class PickerTests(unittest.TestCase):
 def test_catalogue_pagination_and_distinct_venues(self):
  a=item();a['column_values'].append({'id':'address__1','text':'Worcester venue'})
  b=copy.deepcopy(a);b['id']='13060644417';b['column_values'][-1]['text']='Pershore venue'
  cancelled=copy.deepcopy(a);cancelled['id']='33';cancelled['column_values'][2]['text']='Cancelled'
  old=copy.deepcopy(a);old['id']='44';old['column_values'][0]['value']='{"date":"2020-01-01"}';old['column_values'][1]['value']='{"date":"2020-01-02"}'
  calls=[]
  def handler(req):
   body=json.loads(req.content);calls.append(body)
   self.assertNotIn('mutation',body['query'])
   if len(calls)==1:return httpx.Response(200,json={'data':{'boards':[{'id':BOARD,'items_page':{'cursor':'NEXT','items':[a,cancelled,old]}}]}})
   self.assertEqual(body['variables']['cursor'],'NEXT')
   return httpx.Response(200,json={'data':{'next_items_page':{'cursor':None,'items':[b]}}})
  rows=asyncio.run(MondayCourses('test',httpx.MockTransport(handler)).list_courses('2026-11-01','2026-12-01'))
  self.assertEqual({r['id'] for r in rows},{CODE,'13060644417'})
  self.assertEqual({r['venue'] for r in rows},{'Worcester venue','Pershore venue'})
  self.assertEqual(set(rows[0]),{'id','title','client','venue','start','end'})
 def test_catalogue_missing_venue_and_date_limits(self):
  def handler(req):return httpx.Response(200,json={'data':{'boards':[{'id':BOARD,'items_page':{'cursor':None,'items':[item()]}}]}})
  source=MondayCourses('test',httpx.MockTransport(handler))
  rows=asyncio.run(source.list_courses('2026-11-01','2026-12-01'));self.assertEqual(rows[0]['venue'],'')
  for dates in [('invalid','2026-11-01'),('2026-12-01','2026-11-01'),('2026-01-01','2026-12-01')]:
   with self.assertRaises(HTTPException) as e:asyncio.run(source.list_courses(*dates))
   self.assertEqual(e.exception.status_code,400)
 def test_catalogue_api_error_no_partial_results(self):
  def handler(req):return httpx.Response(200,json={'errors':[{'message':'denied'}]})
  with self.assertRaises(HTTPException) as e:asyncio.run(MondayCourses('test',httpx.MockTransport(handler)).list_courses('2026-11-01','2026-12-01'))
  self.assertEqual(e.exception.status_code,503)
 def setup_client(self,folder,access=True):
  env={'MMVA_ATTENDANCE_ENABLED':'true','MMVA_ATTENDANCE_DESTINATION_VERIFIED':'true','MMVA_ATTENDANCE_SECRET':'x'*40,'MMVA_ATTENDANCE_DB':str(Path(folder)/'test.sqlite3'),'MMVA_ATTENDANCE_JOTFORM_API_KEY':'test','MMVA_ATTENDANCE_COURSE_SOURCE':'monday','MMVA_ATTENDANCE_JOTFORM_COURSE_FIELD':'14'}
  if access:env['MMVA_ATTENDANCE_TRAINER_ACCESS_CODE']='test-only-trainer-code'
  app=FastAPI();app.include_router(build_attendance_router(lambda:None))
  return env,TestClient(app,base_url='https://testserver')
 def test_trainer_session_required_for_lists_and_previews(self):
  with tempfile.TemporaryDirectory() as folder:
   env,c=self.setup_client(folder)
   class Source:
    async def list_courses(self,start,end):return [{'id':CODE,'title':'Course','client':'Company','venue':'Venue','start':'2026-11-02','end':'2026-11-03'}]
    async def resolve(self,code,day,enforce_day=True):return dict(id=CODE,title='Course',client='Company',venue='Venue',start='2026-11-02',end='2026-11-03')
   with patch.dict(os.environ,env),patch('mmva_attendance.MondayCourses',return_value=Source()):
    self.assertEqual(c.post('/attendance/trainer/courses',json={}).status_code,401)
    self.assertEqual(c.post('/attendance/trainer/course',json={'code':CODE}).status_code,401)
    self.assertEqual(c.post('/attendance/trainer/login',json={'code':'wrong-code'}).status_code,401)
    r=c.post('/attendance/trainer/login',json={'code':'test-only-trainer-code'});self.assertEqual(r.status_code,200);self.assertIn('HttpOnly',r.headers['set-cookie'])
    self.assertEqual(c.post('/attendance/trainer/courses',json={}).status_code,200)
    self.assertEqual(c.post('/attendance/trainer/course',json={'code':CODE}).json()['venue'],'Venue')
    self.assertEqual(c.post('/attendance/trainer/logout',json={}).status_code,200)
    self.assertEqual(c.post('/attendance/trainer/courses',json={}).status_code,401)
 def test_login_throttling_and_unicode_does_not_crash(self):
  with tempfile.TemporaryDirectory() as folder:
   env,c=self.setup_client(folder)
   with patch.dict(os.environ,env):
    for _ in range(10):self.assertEqual(c.post('/attendance/trainer/login',json={'code':'invalid-🧩'}).status_code,401)
    self.assertEqual(c.post('/attendance/trainer/login',json={'code':'test-only-trainer-code'}).status_code,429)
 def test_access_code_rotation_invalidates_session(self):
  with tempfile.TemporaryDirectory() as folder:
   env,c=self.setup_client(folder)
   with patch.dict(os.environ,env):
    self.assertEqual(c.post('/attendance/trainer/login',json={'code':'test-only-trainer-code'}).status_code,200)
    with patch.dict(os.environ,{'MMVA_ATTENDANCE_TRAINER_ACCESS_CODE':'rotated-test-only-code'}):
     self.assertEqual(c.post('/attendance/trainer/courses',json={}).status_code,401)

if __name__=='__main__':unittest.main()
