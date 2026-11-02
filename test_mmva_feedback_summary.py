import asyncio,copy,json,os,tempfile,unittest
from unittest.mock import patch
import httpx
from fastapi import HTTPException
from mmva_feedback_summary import FeedbackRosters,feedback_status,roster_hash,aggregate
from mmva_completion import CompletionReader,SPECS,report
from test_mmva_completion import SOURCE,CODE,scans,row
import test_mmva_trainer_picker as picker

def complete():
 d=scans();d['registration']['rows']=[row()];d['feedback']['rows']=[row('2')]
 return report(SOURCE,d,[],'2026-11-02')

class FeedbackTests(unittest.TestCase):
 def test_confirmation_is_required_and_roster_changes_relock(self):
  with tempfile.TemporaryDirectory() as folder:
   store=FeedbackRosters(folder+'/db');r=complete();actor={'email':'trainer@example.invalid'}
   self.assertFalse(feedback_status(r,None)['ready'])
   store.confirm(r,actor,0);self.assertTrue(feedback_status(r,store.latest(CODE))['ready'])
   r['delegates'][0]['forms']['feedback']['entries']=[]
   self.assertFalse(feedback_status(r,store.latest(CODE))['ready'])
   r=complete();r['bookingStudentList']=['New Delegate']
   self.assertFalse(feedback_status(r,store.latest(CODE))['confirmed'])
   with self.assertRaises(HTTPException):store.confirm(r,actor,0)
   r['sourceErrors']=['Candidate feedback'];self.assertFalse(feedback_status(r,store.latest(CODE))['ready'])
 def test_unmatched_identity_and_booking_difference_require_review(self):
  r=complete();r['bookingStudentList']=['One Name','Another Name']
  with tempfile.TemporaryDirectory() as folder:
   store=FeedbackRosters(folder+'/db')
   with self.assertRaises(HTTPException):store.confirm(r,{'email':'trainer@example.invalid'},0)
   store.confirm(r,{'email':'trainer@example.invalid'},0,'One booked delegate did not attend.')
   r['unmatched']=[{'form':'Candidate feedback'}];self.assertFalse(feedback_status(r,store.latest(CODE))['ready'])
   r['unmatched']=[];r['delegates'][0]['issues']=['Different names use this email; review identity']
   self.assertFalse(feedback_status(r,store.latest(CODE))['ready'])
 def test_only_selected_course_allowlisted_answers_and_latest_duplicate(self):
  def raw(sid,course,answer):return {'id':sid,'status':'ACTIVE','created_at':'2026-11-02 15:00:00','answers':{'107':{'answer':course},'95':{'answer':'one@example.invalid'},'34':{'answer':{'first':'Test','last':'Delegate'}},'112':{'answer':answer},'25':{'answer':'Improve handouts'},'104':{'answer':'PRIVATE HEALTH'},'999':{'answer':'SECRET'}}}
  payload=[raw('1',CODE,'No'),raw('2',CODE,'Yes'),raw('3','OTHER','Other course secret')]
  def handler(req):return httpx.Response(200,json={'responseCode':200,'content':payload})
  reader=CompletionReader('test',httpx.MockTransport(handler))
  scan=asyncio.run(reader.read_form(SPECS['feedback'],feedback_course=CODE))
  self.assertNotIn('feedback',scan['rows'][2]);self.assertNotIn('PRIVATE HEALTH',json.dumps(scan));self.assertNotIn('SECRET',json.dumps(scan))
  summary=aggregate(scan['rows'],CODE)
  self.assertEqual(summary['responses'],1);self.assertEqual(summary['duplicates'],1);self.assertEqual(summary['ratings'][0]['counts'],{'Yes':1})
  self.assertNotIn('email',json.dumps(summary));self.assertNotIn('Other course secret',json.dumps(summary))
  normal=asyncio.run(reader.read_form(SPECS['feedback']));self.assertNotIn('Improve handouts',json.dumps(normal))
 def test_routes_recheck_auth_confirmation_roster_and_deleted_feedback(self):
  with tempfile.TemporaryDirectory() as folder:
   env,c=picker.PickerTests().setup_client(folder);data=scans();data['registration']['rows']=[row()];data['feedback']['rows']=[row('2')]
   class Source:
    async def resolve(self,*a,**kw):return SOURCE
   class Reader:
    def __init__(self,key):pass
    async def read(self):return copy.deepcopy(data)
    async def read_form(self,*a,**kw):return {'available':True,'rows':[dict(row('2'),feedback={'112':'Yes'})]}
   with patch.dict(os.environ,env),patch('mmva_attendance.MondayCourses',return_value=Source()),patch('mmva_attendance.CompletionReader',Reader):
    endpoint='/attendance/trainer/feedback-summary-data';body={'code':CODE}
    self.assertEqual(c.post(endpoint,json=body).status_code,401)
    c.post('/attendance/trainer/login',json={'email':'trainer@example.invalid','password':'test-only-trainer-password'})
    self.assertEqual(c.post(endpoint,json=body).status_code,409)
    r=c.post('/attendance/trainer/completion',json=body).json();f=r['feedbackSummary']
    confirm=dict(body,confirm=True,version=f['version'],roster='stale')
    self.assertEqual(c.post('/attendance/trainer/feedback-roster',json=confirm).status_code,409)
    confirm['roster']=f['roster'];self.assertEqual(c.post('/attendance/trainer/feedback-roster',json=confirm).status_code,200)
    response=c.post(endpoint,json=body);self.assertEqual(response.status_code,200);self.assertEqual(response.headers['cache-control'],'no-store');self.assertEqual(response.json()['responses'],1)
    data['feedback']['rows']=[];self.assertEqual(c.post(endpoint,json=body).status_code,409)
