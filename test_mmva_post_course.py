import asyncio,copy,json,os,tempfile,unittest
from unittest.mock import patch
import httpx
from mmva_health_followup import post_course_flag,enrich,HealthFollowups,POST_FITNESS_CLEAR,POST_FITNESS_ISSUE
from mmva_completion import project,SPECS,CompletionReader,booking_names_list
from test_mmva_health_followup import answers
from test_mmva_completion import SOURCE,scans,row,report
import test_mmva_trainer_picker as picker

class PostCourseTests(unittest.TestCase):
 def test_flags_and_empty_tables_without_disclosing_details(self):
  for matrix in ['','No injuries',{'1':{'injury':'','report':False}},'[["", false]]']:
   self.assertEqual(post_course_flag(answers(**{'74':POST_FITNESS_CLEAR,'76':matrix}),'k')['state'],'clear')
  for a in [answers(**{'74':POST_FITNESS_ISSUE}),answers(**{'74':POST_FITNESS_CLEAR,'76':{'1':{'injury':'PRIVATE INJURY','report':'Accident Report Sent'}}})]:
   result=post_course_flag(a,'k');self.assertEqual(result['state'],'attention');self.assertNotIn('PRIVATE INJURY',json.dumps(result))
  self.assertEqual(post_course_flag({},'k')['state'],'unknown')
 def test_fitness_projection_and_separate_action_history(self):
  a=answers(**{'78':SOURCE['id'],'11':'one@example.invalid','14':{'first':'Test','last':'Delegate'},'74':POST_FITNESS_ISSUE,'76':'PRIVATE'})
  projected=project({'id':'55','status':'ACTIVE','answers':a},SPECS['fitness'],'k');self.assertNotIn('PRIVATE',json.dumps(projected))
  data=scans();r=row('55');r['health']=projected['health'];data['fitness']['rows']=[r];base=report(SOURCE,data,[],'2026-11-02')
  with tempfile.TemporaryDirectory() as folder:
   store=HealthFollowups(folder+'/notes.sqlite');result=enrich(copy.deepcopy(base),{},form='fitness');self.assertEqual(result['postCourseFollowupOutstanding'],1);self.assertTrue(result['postCourseFollowups'][0]['canRecord'])
   store.save(SOURCE['id'],'55',projected['health']['revision'],'Action taken and incident report arranged.','resolved',{'name':'Test Trainer','email':'test@example.invalid'},0)
   result=enrich(copy.deepcopy(base),store.notes(SOURCE['id']),form='fitness');self.assertFalse(result['postCourseFollowups'][0]['outstanding'])
   base['delegates'][0]['forms']['fitness']['entries'][0]['health']['revision']='c'*64
   self.assertTrue(enrich(base,store.notes(SOURCE['id']),form='fitness')['postCourseFollowups'][0]['outstanding'])
 def test_post_submission_scope_and_route(self):
  def handler(req):return httpx.Response(200,json={'responseCode':200,'content':{'id':'22','form_id':SPECS['health'][0],'status':'ACTIVE','answers':{}}})
  self.assertIsNone(asyncio.run(CompletionReader('k',httpx.MockTransport(handler)).read_post_course_submission('22')))
  with tempfile.TemporaryDirectory() as folder:
   env,c=picker.PickerTests().setup_client(folder)
   class Source:
    async def resolve(self,*args,**kwargs):return SOURCE
   declaration={'course':SOURCE['id'],'health':post_course_flag(answers(**{'74':POST_FITNESS_ISSUE}),'k')}
   class Reader:
    def __init__(self,key):pass
    async def read_post_course_submission(self,sid):return declaration
   data={'code':SOURCE['id'],'submission':'22','revision':declaration['health']['revision'],'version':0,'note':'Follow-up action recorded.','state':'resolved'}
   with patch.dict(os.environ,env),patch('mmva_attendance.MondayCourses',return_value=Source()),patch('mmva_attendance.CompletionReader',Reader):
    self.assertEqual(c.post('/attendance/trainer/post-course-followup',json=data).status_code,401)
    c.post('/attendance/trainer/login',json={'email':'trainer@example.invalid','password':'test-only-trainer-password'})
    self.assertEqual(c.post('/attendance/trainer/post-course-followup',json=data).status_code,200)
    declaration['course']='999';self.assertEqual(c.post('/attendance/trainer/post-course-followup',json=data).status_code,400)
 def test_booking_list_display_without_inferred_identities(self):
  self.assertEqual(booking_names_list('1. John Smith\n2. Jane Jones; Ali Brown'),['John Smith','Jane Jones','Ali Brown'])
  self.assertEqual(booking_names_list('John Smith, Jane Jones'),['John Smith','Jane Jones'])
  self.assertEqual(booking_names_list('Smith, John\nJones, Jane'),['Smith, John','Jones, Jane'])
  self.assertEqual(booking_names_list('Jane Mary Jones'),['Jane Mary Jones'])
  self.assertEqual(booking_names_list(''),[])
  self.assertEqual(booking_names_list('To be confirmed'),[])
