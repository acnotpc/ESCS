import asyncio,copy,json,os,tempfile,unittest
from unittest.mock import patch
import httpx
from fastapi import HTTPException
from mmva_completion import CompletionReader,SPECS,project,report
from test_mmva_monday_courses import item,CODE,reader
import test_mmva_trainer_picker as picker

SOURCE=dict(trainer='Test Trainer',trainerEmails=['trainer@example.invalid'],knowledgeRequired=True,courseOption=1,id=CODE,title='Test course',client='Test company',venue='Test venue',start='2026-11-02',end='2026-11-02')
def scans():return {k:{'available':True,'rows':[]} for k in SPECS}
def row(sid='1',course=CODE,email='one@example.invalid',name='Test Delegate'):
 return dict(id=sid,course=course,email=email,name=name,created='2026-11-02 09:00:00')
def raw(sid='1'):
 return dict(id=sid,status='ACTIVE',created_at='2026-11-02 09:00:00',answers={'63':{'answer':CODE},'11':{'answer':'ONE@EXAMPLE.INVALID'},'14':{'answer':{'first':'Test','last':'Delegate'}},'999':{'answer':'CONFIDENTIAL MEDICAL ANSWER'}})
class CompletionTests(unittest.TestCase):
 def test_projection_strips_health_answers_and_deleted_rows(self):
  p=project(raw(),SPECS['registration']);self.assertEqual(p['email'],'one@example.invalid');self.assertEqual(p['name'],'Test Delegate');self.assertNotIn('CONFIDENTIAL',json.dumps(p))
  r=raw();r['status']='DELETED';self.assertIsNone(project(r,SPECS['registration']))
 def test_pagination_and_failure_never_return_partial_success(self):
  offsets=[]
  def handler(req):
   self.assertEqual(req.method,'GET');self.assertEqual(req.headers['APIKEY'],'test');offset=int(req.url.params['offset']);offsets.append(offset)
   return httpx.Response(200,json={'responseCode':200,'resultSet':{'limit':2},'content':[raw('1'),raw('2')] if offset==0 else [raw('3')]})
  result=asyncio.run(CompletionReader('test',httpx.MockTransport(handler),page_size=2).read_form(SPECS['registration']))
  self.assertTrue(result['available']);self.assertEqual(offsets,[0,2]);self.assertEqual(len(result['rows']),3)
  def fails(req):
   if req.url.params['offset']=='0':return handler(req)
   return httpx.Response(503,json={})
  result=asyncio.run(CompletionReader('test',httpx.MockTransport(fails),page_size=2).read_form(SPECS['registration']))
  self.assertEqual(result,{'available':False,'rows':[]})
 def test_exact_course_matching_and_duplicate_identity_review(self):
  data=scans();data['registration']['rows']=[row('1'),row('2',course='123'),row('3',email='',name='No email')]
  data['health']['rows']=[row('4',name='Other Name'),row('5')]
  data['fitness']['available']=False
  data['feedback']['rows']=[row('6',course='')]
  result=report(SOURCE,data,[],'2026-11-02');self.assertEqual(len(result['delegates']),1);p=result['delegates'][0]
  self.assertEqual(p['forms']['registration']['state'],'received');self.assertEqual(p['forms']['fitness']['state'],'unavailable');self.assertFalse(p['complete']);self.assertEqual(len(result['unmatched']),2);self.assertEqual(len(p['issues']),2)
  self.assertNotIn('123',json.dumps(result['delegates']))
 def test_all_forms_and_attendance_without_certification_claim(self):
  data=scans()
  for key in ('registration','health','fitness','knowledge','feedback'):data[key]['rows']=[row()]
  attendance=[dict(email='one@example.invalid',name='Test Delegate',day='2026-11-02',step=i,time=1793610000+i*3600,sync='sent') for i in range(4)]
  p=report(SOURCE,data,attendance,'2026-11-02')['delegates'][0];self.assertTrue(p['complete']);self.assertNotIn('passed',p)
  future=dict(SOURCE,end='2026-11-03');p=report(future,data,attendance,'2026-11-02')['delegates'][0];self.assertFalse(p['complete']);self.assertTrue(p['attendance'][1]['future'])
 def test_finished_course_read_review_does_not_open_attendance(self):
  finished=item();finished['column_values'][2]['text']='Course Complete'
  with self.assertRaises(HTTPException):asyncio.run(reader(finished).resolve(CODE,'2026-11-02'))
  self.assertEqual(asyncio.run(reader(finished).resolve(CODE,'2026-11-02',enforce_day=False,review=True))['id'],CODE)
 def test_trainer_endpoint_auth_and_minimal_report(self):
  with tempfile.TemporaryDirectory() as folder:
   env,c=picker.PickerTests().setup_client(folder)
   class Source:
    async def resolve(self,*args,**kwargs):return SOURCE
   class Reader:
    def __init__(self,key):pass
    async def read(self):return scans()
   with patch.dict(os.environ,env),patch('mmva_attendance.MondayCourses',return_value=Source()),patch('mmva_attendance.CompletionReader',Reader):
    self.assertEqual(c.post('/attendance/trainer/completion',json={'code':CODE}).status_code,401)
    c.post('/attendance/trainer/login',json={'email':'trainer@example.invalid','password':'test-only-trainer-password'})
    r=c.post('/attendance/trainer/completion',json={'code':CODE});self.assertEqual(r.status_code,200);self.assertEqual(r.headers['cache-control'],'no-store');self.assertEqual(r.json()['delegates'],[])
    self.assertNotIn('APIKEY',r.text);self.assertNotIn('password_hash',r.text)
