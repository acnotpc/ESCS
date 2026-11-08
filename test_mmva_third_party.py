import copy,os,tempfile,unittest
from unittest.mock import patch
from itsdangerous import URLSafeTimedSerializer
from mmva_management_records import ManagementRecords,closeout_blockers
from mmva_trainer_auth import TrainerAccounts
from mmva_management import LABELS
from test_mmva_completion import SOURCE
from test_mmva_management import NOEL

def imported(code):
 return dict(id=code,monday='https://exclusive-gang.monday.com/boards/4727626523/pulses/'+code,notes='THIRD-PARTY COURSE — STAFFING ONLY. Provider manages all delegate paperwork.',checks=[dict(label=l,state='Not required',note='Provider owns paperwork') for l in ['Attendance evidence','Feedback','Certificates','Supporting records']])

class ThirdPartyTests(unittest.TestCase):
 def test_policy_requires_explicit_source_note_and_all_four_exceptions(self):
  with tempfile.TemporaryDirectory() as folder:
   db=ManagementRecords(folder+'/db');record=imported('123')
   db.import_legacy([record],{'email':NOEL});self.assertEqual(db.delivery_policy('123')['mode'],'third_party');self.assertEqual(db.delivery_policy('456')['mode'],'mmva')
   changed=copy.deepcopy(record);changed['checks'][0]['state']='Unverified';db.import_legacy([changed],{'email':NOEL});self.assertEqual(db.delivery_policy('123')['mode'],'mmva')
 def test_staffing_closeout_requires_delivery_confirmation_without_delegate_records(self):
  source=dict(SOURCE,start='2026-01-01',end='2026-01-01');policy=dict(mode='third_party');checks=[dict(label=LABELS[0],state='Verified',note='Allocation checked')]+[dict(label=l,state='Not required',note='Provider manages paperwork') for l in LABELS[1:]];management=dict(leader='Noel',followup='',checks=checks)
  self.assertEqual(closeout_blockers(source,{},management,[],None,'2026-01-02',policy),[])
  self.assertTrue(closeout_blockers(source,{},management,[],None,'2025-12-31',policy))
  self.assertTrue(closeout_blockers(source,{},dict(management,followup='Confirm delivered'),[],None,'2026-01-02',policy))
  self.assertTrue(closeout_blockers(dict(source,trainerEmails=[]),{},management,[],None,'2026-01-02',policy))
 def test_existing_links_and_registration_blocked_but_trainer_can_see_exception(self):
  from test_mmva_trainer_picker import PickerTests
  with tempfile.TemporaryDirectory() as folder:
   env,c=PickerTests().setup_client(folder);source=dict(SOURCE,completed=False)
   ManagementRecords(env['MMVA_ATTENDANCE_DB']).import_legacy([imported(source['id'])],{'email':NOEL})
   class Reader:
    async def resolve(self,*a,**kw):return source
   with patch.dict(os.environ,env),patch('mmva_attendance.MondayCourses',return_value=Reader()):
    c.post('/attendance/trainer/login',json={'email':'trainer@example.invalid','password':'test-only-trainer-password'})
    result=c.post('/attendance/trainer/course',json={'code':source['id']});self.assertEqual(result.status_code,200);self.assertTrue(result.json()['thirdPartyPaperwork']);self.assertEqual(result.json()['forms'],[])
    for path in ['course-info','register']:
     r=c.post('/attendance/'+path,json={'code':source['id']});self.assertEqual(r.status_code,400,r.text);self.assertIn('staffing-only',r.text)
    signer=URLSafeTimedSerializer(env['MMVA_ATTENDANCE_SECRET'],salt='mmva-course-form-v1');token=signer.dumps({'course':source['id'],'form':'feedback'})
    r=c.get('/attendance/course-form?token='+token,follow_redirects=False);self.assertEqual(r.status_code,400)
 def test_actions_skip_third_party_delegate_requirements(self):
  from test_mmva_trainer_picker import PickerTests
  with tempfile.TemporaryDirectory() as folder:
   env,c=PickerTests().setup_client(folder);source=dict(SOURCE,completed=False,status='Booked',url='https://example.invalid')
   ManagementRecords(env['MMVA_ATTENDANCE_DB']).import_legacy([imported(source['id'])],{'email':NOEL});accounts=TrainerAccounts(env['MMVA_ATTENDANCE_DB'],env['MMVA_ATTENDANCE_SECRET']);invite=accounts.invite(NOEL,'Noel Pusey');accounts.setup(invite['setup_path'].split('token=')[1],'noel-test-only-password','test')
   class Reader:
    async def resolve(self,*a,**kw):return source
    async def list_courses(self,*a,**kw):return [source]
   class Scan:
    def __init__(self,*a):pass
    async def read(self):return {}
   with patch.dict(os.environ,env),patch('mmva_attendance.MondayCourses',return_value=Reader()),patch('mmva_attendance.CompletionReader',Scan):
    c.post('/attendance/trainer/login',json={'email':NOEL,'password':'noel-test-only-password'})
    r=c.post('/attendance/trainer/management-actions',json={});self.assertEqual(r.status_code,200,r.text);row=r.json()['courses'][0];self.assertTrue(row['thirdPartyPaperwork']);self.assertNotIn('missingAttendance',row)
    self.assertEqual(c.post('/attendance/trainer/management-certificate',json={'code':source['id']}).status_code,400)
