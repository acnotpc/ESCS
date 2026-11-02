import asyncio,copy,json,os,tempfile,unittest
from unittest.mock import patch
import httpx
from mmva_monday_courses import MondayCourses,assigned_emails,trainer_allowed,TRAINER_EMAIL_COLUMN,COURSE_LEADER
from mmva_trainer_auth import TrainerAccounts
from test_mmva_monday_courses import item,CODE
from test_mmva_completion import SOURCE,scans
import test_mmva_trainer_picker as picker

class CourseAccessTests(unittest.TestCase):
 def test_exact_email_assignment_name_required_and_leader_override(self):
  source=dict(SOURCE,trainerEmails=assigned_emails(' ZOE.CHIVERS@securecareservices.co.uk\ntrainer@example.invalid; other@example.invalid'))
  self.assertTrue(trainer_allowed(source,'Zoe.Chivers@securecareservices.co.uk'));self.assertFalse(trainer_allowed(source,'zoe.chivers@securecareservices.co.uk.evil.invalid'))
  source['trainer']='';self.assertFalse(trainer_allowed(source,'trainer@example.invalid'));self.assertTrue(trainer_allowed(source,'Andy@securecareservices.co.uk'))
  self.assertEqual(assigned_emails('trainer@example.invalid extra words'),[]);self.assertEqual(assigned_emails(''),[])
 def test_list_filters_before_returning_course_metadata(self):
  a=item();a['column_values'].append({'id':TRAINER_EMAIL_COLUMN,'text':'trainer@example.invalid'})
  b=copy.deepcopy(a);b['id']='12345';b['column_values'][-1]['text']='other@example.invalid'
  def handle(req):
   self.assertIn(TRAINER_EMAIL_COLUMN,json.loads(req.content)['query'])
   return httpx.Response(200,json={'data':{'boards':[{'id':'4727626523','items_page':{'cursor':None,'items':[a,b]}}]}})
  reader=MondayCourses('test',httpx.MockTransport(handle))
  rows=asyncio.run(reader.list_courses('2026-11-01','2026-11-04',trainer_email='trainer@example.invalid'));self.assertEqual([r['id'] for r in rows],[CODE]);self.assertNotIn('trainerEmails',rows[0])
  self.assertEqual(len(asyncio.run(reader.list_courses('2026-11-01','2026-11-04',trainer_email=COURSE_LEADER))),2)
  self.assertEqual(asyncio.run(reader.list_courses('2026-11-01','2026-11-04',trainer_email='unknown@example.invalid')),[])
 def test_every_sensitive_read_and_write_checks_fresh_assignment(self):
  routes=['course','completion','submission-data','attendance-review','historic-attendance-day','verify-names','combine-delegates','feedback-roster','feedback-summary-data','health-followup','post-course-followup']
  with tempfile.TemporaryDirectory() as folder:
   env,c=picker.PickerTests().setup_client(folder);source=dict(SOURCE,trainerEmails=[])
   class Source:
    async def resolve(self,*a,**kw):return source
   class Reader:
    def __init__(self,key):pass
    async def read(self):return scans()
   with patch.dict(os.environ,env),patch('mmva_attendance.MondayCourses',return_value=Source()),patch('mmva_attendance.CompletionReader',Reader):
    c.post('/attendance/trainer/login',json={'email':'trainer@example.invalid','password':'test-only-trainer-password'})
    for path in routes:
     r=c.post('/attendance/trainer/'+path,json={'code':CODE,'confirm':True});self.assertEqual(r.status_code,403,(path,r.text))
    source['trainerEmails']=['trainer@example.invalid'];self.assertEqual(c.post('/attendance/trainer/completion',json={'code':CODE}).status_code,200)
    source['trainerEmails']=[];self.assertEqual(c.post('/attendance/trainer/completion',json={'code':CODE}).status_code,403) # Cached records cannot bypass revoked assignment.
 def test_leader_can_access_unassigned_course_with_active_login(self):
  with tempfile.TemporaryDirectory() as folder:
   env,c=picker.PickerTests().setup_client(folder);accounts=TrainerAccounts(env['MMVA_ATTENDANCE_DB'],env['MMVA_ATTENDANCE_SECRET']);invite=accounts.invite(COURSE_LEADER,'Course Leader');accounts.setup(invite['setup_path'].split('token=')[1],'leader-test-only-password','test')
   class Source:
    async def resolve(self,*a,**kw):return dict(SOURCE,trainer='',trainerEmails=[])
   class Reader:
    def __init__(self,key):pass
    async def read(self):return scans()
   with patch.dict(os.environ,env),patch('mmva_attendance.MondayCourses',return_value=Source()),patch('mmva_attendance.CompletionReader',Reader):
    self.assertEqual(c.post('/attendance/trainer/login',json={'email':COURSE_LEADER,'password':'leader-test-only-password'}).status_code,200)
    self.assertEqual(c.post('/attendance/trainer/course',json={'code':CODE}).status_code,200);self.assertEqual(c.post('/attendance/trainer/completion',json={'code':CODE}).status_code,200)
