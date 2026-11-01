import asyncio
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from mmva_monday_courses import MondayCourses, BOARD
from mmva_attendance import Ledger, build_attendance_router, london_day

CODE='13436327815'
def item():
 return {'id':CODE,'name':'Course','state':'active','board':{'id':BOARD},'column_values':[
  {'id':'date','value':'{"date":"2026-11-02","time":"09:00:00"}','text':'2026-11-02 09:00'},
  {'id':'date2','value':'{"date":"2026-11-03"}','text':'2026-11-03'},
  {'id':'status','text':'Booked'}, {'id':'text34','text':'Test Company'},
  {'id':'type_of_training','text':'Test course'}, {'id':'dropdown4','text':'Test Trainer'}]}

def reader(row=None, errors=False):
 def handle(req):
  data=json.loads(req.content)
  assert data['variables']['ids']==[CODE]
  assert 'mutation' not in data['query']
  return httpx.Response(200,json={'errors':[{'message':'denied'}]} if errors else {'data':{'items':[row or item()]}})
 return MondayCourses('test',httpx.MockTransport(handle))

class MondayTests(unittest.TestCase):
 def test_exact_id_dates_and_title(self):
  r=asyncio.run(reader().resolve(CODE,'2026-11-02'))
  self.assertEqual(r['id'],CODE);self.assertEqual(r['title'],'Test Company — Test course')
  asyncio.run(reader().resolve(CODE,'2026-11-03'))
  with self.assertRaises(HTTPException):asyncio.run(reader().resolve(CODE,'2026-11-04'))
  with self.assertRaises(HTTPException):asyncio.run(reader().resolve(CODE,'2026-11-01'))
  self.assertEqual(asyncio.run(reader().resolve(CODE,'2026-11-01',False))['start'],'2026-11-02')
 def test_cancelled_completed_unknown_status(self):
  for status in ['Cancelled','Course Complete','Not Required','New status','']:
   row=item();row['column_values'][2]['text']=status
   with self.assertRaises(HTTPException):asyncio.run(reader(row).resolve(CODE,'2026-11-02'))
 def test_wrong_board_archived_and_mismatched_id(self):
  for change in [{'board':{'id':'1'}},{'state':'archived'},{'id':'999'}]:
   row=item();row.update(change)
   with self.assertRaises(HTTPException):asyncio.run(reader(row).resolve(CODE,'2026-11-02'))
 def test_invalid_input_and_unavailable_fail_closed(self):
  for code in ['QATEST01','123 OR 1','', '123:2026-11-02']:
   with self.assertRaises(HTTPException):asyncio.run(reader().resolve(code,'2026-11-02'))
  with self.assertRaises(HTTPException) as e:asyncio.run(reader(errors=True).resolve(CODE,'2026-11-02'))
  self.assertEqual(e.exception.status_code,503)
 def test_missing_and_reversed_dates(self):
  for date_value in ['{}','{"date":"invalid"}','{"date":"2026-12-02"}']:
   row=item();row['column_values'][0]['value']=date_value
   with self.assertRaises(HTTPException):asyncio.run(reader(row).resolve(CODE,'2026-11-02'))
 def test_ledger_day_reset_and_explicit_course_field(self):
  with tempfile.TemporaryDirectory() as tmp:
   db=Ledger(str(Path(tmp)/'qa.sqlite3'),'x'*40)
   a=asyncio.run(reader().resolve(CODE,'2026-11-02')); b=dict(a,day='2026-11-03')
   code_a=db.monday_course(a);code_b=db.monday_course(b)
   data={'first':'Test','last':'Delegate','email':'test@example.invalid','organisation':'TEST'}
   from datetime import datetime
   now=datetime.fromisoformat('2026-11-02T12:00:00+00:00').timestamp()
   p1=db.enrol(dict(data,code=code_a),now);p2=db.enrol(dict(data,code=code_b),now+86400)
   event=db.record(p1,0,now)
   self.assertEqual(db.status(p2,now+86400)['step'],0)
   self.assertEqual(db.status(p1,now)['courseNumber'],CODE)
   def send(req):
    payload=parse_qs(req.content.decode());self.assertEqual(payload['submission[14]'],[CODE])
    self.assertIn(CODE,payload['submission[10]'][0])
    return httpx.Response(200,json={'responseCode':200,'content':{'submissionID':'12345'}})
   with patch.dict(os.environ,{'MMVA_ATTENDANCE_JOTFORM_COURSE_FIELD':'14'}):asyncio.run(db.deliver(event,'test',httpx.MockTransport(send)))
 def test_monday_routes_cannot_create_random_codes_and_recheck_cancellation(self):
  with tempfile.TemporaryDirectory() as tmp:
   env={'MMVA_ATTENDANCE_ENABLED':'true','MMVA_ATTENDANCE_DESTINATION_VERIFIED':'true','MMVA_ATTENDANCE_SECRET':'x'*40,'MMVA_ATTENDANCE_DB':str(Path(tmp)/'db.sqlite3'),'MMVA_ATTENDANCE_JOTFORM_API_KEY':'test','MMVA_ATTENDANCE_COURSE_SOURCE':'monday','MMVA_ATTENDANCE_JOTFORM_COURSE_FIELD':'14'}
   now=1793620800  # 2 November 2026 12:00 UTC
   from datetime import datetime
   now=datetime.fromisoformat('2026-11-02T12:00:00+00:00').timestamp()
   with patch.dict(os.environ,env),patch('mmva_attendance.time.time',return_value=now),patch('mmva_attendance.MondayCourses',return_value=reader()):
    app=FastAPI();app.include_router(build_attendance_router(lambda:None));c=TestClient(app,base_url='https://testserver')
    self.assertEqual(c.post('/attendance/admin/courses',json={}).status_code,409)
    s=c.post('/attendance/register',json={'code':CODE,'first':'Test','last':'Delegate','email':'test@example.invalid','organisation':'TEST'})
    self.assertEqual(s.status_code,200);self.assertEqual(s.json()['courseNumber'],CODE)
    cancelled=item();cancelled['column_values'][2]['text']='Cancelled'
    with patch('mmva_attendance.MondayCourses',return_value=reader(cancelled)):
     self.assertEqual(c.post('/attendance/record',json={'token':s.json()['token'],'confirm':True}).status_code,400)
     self.assertEqual(c.get('/attendance/status').status_code,400)

if __name__=='__main__':unittest.main()
