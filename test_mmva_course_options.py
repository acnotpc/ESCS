import asyncio,copy,os,tempfile,unittest
from unittest.mock import patch
from mmva_monday_courses import parse_course
from test_mmva_monday_courses import item,CODE
from test_mmva_completion import SOURCE,scans,row,report
import test_mmva_trainer_picker as picker

class CourseOptionTests(unittest.TestCase):
 def test_numbered_options_not_internal_status_indexes(self):
  for option in range(1,11):
   source=item();source['column_values'].append({'id':'type_of_training','text':str(option)+' - Course description'})
   parsed=parse_course(source,CODE,'2026-11-02')
   self.assertEqual(parsed['courseOption'],option)
   self.assertEqual(parsed['knowledgeRequired'],option in {1,4,10})
  source=item();source['column_values'].append({'id':'type_of_training','text':'Unnumbered legacy course'})
  self.assertIsNone(parse_course(source,CODE,'2026-11-02')['knowledgeRequired'])
 def test_other_courses_do_not_need_knowledge_even_when_source_unavailable(self):
  source=dict(SOURCE,knowledgeRequired=False,courseOption=5);data=scans()
  for key in ('registration','health','fitness','feedback'):data[key]['rows']=[row()]
  data['knowledge']['available']=False
  attendance=[dict(email='one@example.invalid',name='Test Delegate',day='2026-11-02',step=i,time=1793610000+i*3600,sync='sent') for i in range(4)]
  r=report(source,data,attendance,'2026-11-02');p=r['delegates'][0]
  self.assertEqual(p['forms']['knowledge']['state'],'not_required');self.assertTrue(p['complete']);self.assertNotIn('Knowledge assessment',r['sourceErrors'])
  source['knowledgeRequired']=True;self.assertFalse(report(source,data,attendance,'2026-11-02')['delegates'][0]['complete'])
  source['knowledgeRequired']=None;self.assertTrue(report(source,data,attendance,'2026-11-02')['delegates'][0]['issues'])
 def test_qr_selector_omits_nonrequired_assessment_and_old_link_is_checked(self):
  from itsdangerous import URLSafeTimedSerializer
  with tempfile.TemporaryDirectory() as folder:
   env,c=picker.PickerTests().setup_client(folder)
   source=dict(SOURCE,knowledgeRequired=False,courseOption=5)
   class Reader:
    async def resolve(self,*args,**kwargs):return source
   with patch.dict(os.environ,env),patch('mmva_attendance.MondayCourses',return_value=Reader()):
    c.post('/attendance/trainer/login',json={'email':'trainer@example.invalid','password':'test-only-trainer-password'})
    r=c.post('/attendance/trainer/course',json={'code':CODE}).json();self.assertNotIn('knowledge',[f['key'] for f in r['forms']])
    token=URLSafeTimedSerializer(env['MMVA_ATTENDANCE_SECRET'],salt='mmva-course-form-v1').dumps({'course':CODE,'form':'knowledge'})
    self.assertEqual(c.get('/attendance/course-form',params={'token':token},follow_redirects=False).status_code,400)
    source['knowledgeRequired']=True;source['courseOption']=10
    r=c.post('/attendance/trainer/course',json={'code':CODE}).json();self.assertIn('knowledge',[f['key'] for f in r['forms']])
    self.assertEqual(c.get('/attendance/course-form',params={'token':token},follow_redirects=False).status_code,302)
