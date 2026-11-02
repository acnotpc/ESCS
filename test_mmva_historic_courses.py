import asyncio,copy,json,os,tempfile,unittest
from unittest.mock import patch
import httpx
from fastapi import HTTPException
from mmva_monday_courses import MondayCourses,parse_course,TRAINER_EMAIL_COLUMN
from mmva_attendance import Ledger
from mmva_attendance_review import AttendanceReviews,enrich_attendance
from test_mmva_monday_courses import item,CODE
from test_mmva_completion import SOURCE,scans,row,report
import test_mmva_trainer_picker as picker

class HistoricCourseTests(unittest.TestCase):
 def test_past_ongoing_course_is_completed_by_dates_and_cancelled_stays_hidden(self):
  past=item();past['id']='13386867986';past['column_values'][0]['value']='{"date":"2026-10-26"}';past['column_values'][1]['value']='{"date":"2026-10-30"}';past['column_values'][2]['text']='Course Ongoing'
  future=item();future['id']='999';future['column_values'][0]['value']='{"date":"2026-11-09"}';future['column_values'][1]['value']='{"date":"2026-11-10"}'
  cancelled=copy.deepcopy(past);cancelled['id']='888';cancelled['column_values'][2]['text']='Cancelled'
  for r in [past,future,cancelled]:r['column_values'].append({'id':TRAINER_EMAIL_COLUMN,'text':'trainer@example.invalid'})
  def handler(req):return httpx.Response(200,json={'data':{'boards':[{'id':'4727626523','items_page':{'cursor':None,'items':[past,future,cancelled]}}]}})
  reader=MondayCourses('test',httpx.MockTransport(handler))
  def listing(category):return asyncio.run(reader.list_courses('2026-10-01','2026-11-30',review=True,trainer_email='trainer@example.invalid',category=category,on_day='2026-11-02'))
  self.assertEqual([r['id'] for r in listing('completed')],['13386867986']);self.assertTrue(listing('completed')[0]['completed'])
  self.assertEqual([r['id'] for r in listing('upcoming')],['999']);self.assertEqual(len(listing('all')),2)
  self.assertEqual(past['column_values'][2]['text'],'Course Ongoing') # Grouping does not change source delivery or qualification status.
 def test_categories_use_past_range_and_completed_course_preview_is_read_only(self):
  with tempfile.TemporaryDirectory() as folder:
   env,c=picker.PickerTests().setup_client(folder);calls=[]
   class Source:
    async def list_courses(self,start,end,**kw):calls.append((start,end,kw));return []
    async def resolve(self,*a,**kw):return dict(SOURCE,completed=True)
   with patch.dict(os.environ,env),patch('mmva_attendance.MondayCourses',return_value=Source()),patch('mmva_attendance.london_day',return_value='2026-11-02'):
    c.post('/attendance/trainer/login',json={'email':'trainer@example.invalid','password':'test-only-trainer-password'})
    for route in ['courses','review-courses']:
     r=c.post('/attendance/trainer/'+route,json={'category':'completed'});self.assertEqual(r.status_code,200);self.assertEqual(r.json()['until'],'2026-11-02');self.assertEqual(r.json()['category'],'completed');self.assertEqual(calls[-1][2]['trainer_email'],'trainer@example.invalid');self.assertTrue(calls[-1][2]['review'])
    self.assertEqual(c.post('/attendance/trainer/courses',json={'category':'invalid'}).status_code,400)
    self.assertTrue(c.post('/attendance/trainer/course',json={'code':CODE}).json()['completed'])
 def test_historic_day_enables_evidence_without_creating_attendance(self):
  with tempfile.TemporaryDirectory() as folder:
   path=folder+'/db';ledger=Ledger(path,'x');data=scans();data['registration']['rows']=[row()];raw=report(SOURCE,data,[],'2026-11-03');store=AttendanceReviews(path);actor={'email':'trainer@example.invalid','name':'Trainer'}
   self.assertEqual(enrich_attendance(copy.deepcopy(raw),path,'2026-11-03')['attendanceReviews'],[])
   for day in ['2026-11-03','2026-11-04','2026-11-01']:
    with self.assertRaises(HTTPException):store.open_day(SOURCE,day,'2026-11-03',actor,'Original register available')
   with self.assertRaises(HTTPException):store.open_day(SOURCE,'2026-11-02','2026-11-03',actor,'')
   store.open_day(SOURCE,'2026-11-02','2026-11-03',actor,'Original paper register reference ABC')
   r=enrich_attendance(copy.deepcopy(raw),path,'2026-11-03');self.assertEqual(len(r['attendanceReviews']),4);self.assertTrue(all(c['state']=='missing' for c in r['attendanceReviews']));self.assertFalse(r['delegates'][0]['complete']);self.assertEqual(r['historicalAttendanceDays'][0]['actor'],actor['email'])
   with ledger.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM event').fetchone()[0],0)
 def test_historic_day_endpoint_requires_sign_in_and_confirmation(self):
  with tempfile.TemporaryDirectory() as folder:
   env,c=picker.PickerTests().setup_client(folder)
   class Source:
    async def resolve(self,*a,**kw):return SOURCE
   class Reader:
    def __init__(self,key):pass
    async def read(self):
     data=scans();data['registration']['rows']=[row()];return data
   with patch.dict(os.environ,env),patch('mmva_attendance.MondayCourses',return_value=Source()),patch('mmva_attendance.CompletionReader',Reader),patch('mmva_attendance.london_day',return_value='2026-11-03'):
    url='/attendance/trainer/historic-attendance-day';data={'code':CODE,'day':'2026-11-02','reason':'Register reference ABC','confirm':True}
    self.assertEqual(c.post(url,json=data).status_code,401)
    c.post('/attendance/trainer/login',json={'email':'trainer@example.invalid','password':'test-only-trainer-password'})
    data['confirm']=False;self.assertEqual(c.post(url,json=data).status_code,400);data['confirm']=True
    r=c.post(url,json=data);self.assertEqual(r.status_code,200);self.assertEqual(len(r.json()['attendanceReviews']),4)
