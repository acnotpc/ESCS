import copy,json,os,tempfile,unittest
from unittest.mock import patch
from fastapi import HTTPException
from mmva_management_tools import FollowupTasks,evidence_html
from mmva_trainer_auth import TrainerAccounts
from test_mmva_trainer_picker import PickerTests
from test_mmva_completion import SOURCE
from test_mmva_management import NOEL

class ToolsTests(unittest.TestCase):
 def test_actions_validate_owner_completion_and_keep_versions(self):
  with tempfile.TemporaryDirectory() as f:
   store=FollowupTasks(f+'/db');actor={'email':NOEL};accounts=[actor]
   p=dict(title='Check evidence',owner=NOEL,due='2026-11-09',status='Open',note='',evidence='',version=0)
   first=store.save('123',p,actor,accounts)
   for changed in [dict(owner='unknown@example.invalid'),dict(due='invalid'),dict(due='20261109'),dict(status='Done'),dict(evidence='javascript:alert(1)')]:
    with self.assertRaises(HTTPException):store.save('123',dict(p,**changed),actor,accounts)
   update=dict(p,id=first['id'],version=1,status='Done',note='Verified original evidence')
   store.save('123',update,actor,accounts)
   with self.assertRaises(HTTPException) as err:store.save('123',update,actor,accounts)
   self.assertEqual(err.exception.status_code,409);self.assertEqual(len(store.history('123')),2);self.assertEqual(store.latest('123')[0]['status'],'Done')
 def test_export_escapes_content_and_omits_raw_answers(self):
  source=dict(SOURCE,url='https://example.invalid',client='<script>alert(1)</script>')
  report=dict(checkedAt='2026-11-08',delegates=[dict(name='<img onerror=bad>',email='a@example.invalid',forms={},attendance=[],outstanding=[],issues=[],rawHealth='private medical answer')])
  result=evidence_html(source,report,None,[],[],dict(mode='mmva'),dict(name='Manager',email=NOEL),'2026-11-08')
  self.assertIn('&lt;script&gt;',result);self.assertNotIn('<script>',result);self.assertNotIn('private medical answer',result);self.assertIn('full expected roster',result)
 def test_account_diagnostics_exclude_secrets_and_record_completed_reset(self):
  with tempfile.TemporaryDirectory() as f:
   a=TrainerAccounts(f+'/db','x'*40);invite=a.invite('test@example.invalid','Test');token=invite['setup_path'].split('token=')[1];password='test password only 12345';a.setup(token,password,'test');old=a.authenticate(invite['email'],password,'test')
   reset=a.request_reset(invite['email'],'reset');reset_token=reset['reset_path'].split('token=')[1];a.reset_password(reset_token,'different test password 123','test')
   d=a.diagnostics();serialized=json.dumps(d)
   for secret in [token,reset_token,password,'password_hash','reset_hash','invite_hash']:self.assertNotIn(secret,serialized)
   self.assertTrue(d['accounts'][0]['lastPasswordChangeAt']);self.assertTrue(d['accounts'][0]['lastLoginAt'])
   with self.assertRaises(HTTPException):a.check(old)
 def test_routes_protect_accounts_tasks_exports_and_manager_disable(self):
  with tempfile.TemporaryDirectory() as f:
   env,c=PickerTests().setup_client(f)
   with patch.dict(os.environ,env):
    routes=['management-accounts','management-account-action','management-task']
    for route in routes:self.assertEqual(c.post('/attendance/trainer/'+route,json={}).status_code,401)
    self.assertEqual(c.get('/attendance/trainer/management-export?code=123').status_code,401)
    c.post('/attendance/trainer/login',json=dict(email='trainer@example.invalid',password='test-only-trainer-password'))
    for route in routes:self.assertEqual(c.post('/attendance/trainer/'+route,json={}).status_code,403)
    self.assertEqual(c.get('/attendance/trainer/management-export?code=123').status_code,403)
    a=TrainerAccounts(env['MMVA_ATTENDANCE_DB'],env['MMVA_ATTENDANCE_SECRET']);i=a.invite(NOEL,'Noel');a.setup(i['setup_path'].split('token=')[1],'noel-test-only-password','test');c.post('/attendance/trainer/login',json=dict(email=NOEL,password='noel-test-only-password'))
    response=c.post('/attendance/trainer/management-accounts',json={});self.assertEqual(response.status_code,200);self.assertNotIn('password_hash',response.text)
    for email in [NOEL,'andy@securecareservices.co.uk']:self.assertEqual(c.post('/attendance/trainer/management-account-action',json=dict(action='disable',email=email,confirm=True)).status_code,400)
    self.assertEqual(c.post('/attendance/trainer/management-account-action',json=dict(action='disable',email='trainer@example.invalid',confirm=True)).status_code,200)
 def test_dashboard_cache_force_scope_and_task_invalidation(self):
  with tempfile.TemporaryDirectory() as f:
   env,c=PickerTests().setup_client(f);a=TrainerAccounts(env['MMVA_ATTENDANCE_DB'],env['MMVA_ATTENDANCE_SECRET']);i=a.invite(NOEL,'Noel');a.setup(i['setup_path'].split('token=')[1],'noel-test-only-password','test')
   source=dict(SOURCE,status='Booked',url='https://example.invalid');count={'read':0};state={'failed':False}
   class Reader:
    async def resolve(self,*args,**kw):return source
    async def list_courses(self,*args,**kw):return [source]
   class Completion:
    def __init__(self,*args):pass
    async def read(self):
     count['read']+=1
     if state['failed']:raise HTTPException(503,'Unavailable')
     return {}
   base=dict(delegates=[],unmatched=[],sourceErrors=[],courseForms={})
   with patch.dict(os.environ,env),patch('mmva_attendance.MondayCourses',return_value=Reader()),patch('mmva_attendance.CompletionReader',Completion),patch('mmva_attendance.completion_report',return_value=base),patch('mmva_attendance.apply_name_reviews',side_effect=lambda r,*args:r),patch('mmva_attendance.enrich_attendance',side_effect=lambda r,*args:r),patch('mmva_attendance.feedback_status',return_value={}):
    c.post('/attendance/trainer/login',json=dict(email=NOEL,password='noel-test-only-password'))
    def scan(data={}):
     r=c.post('/attendance/trainer/management-actions',json=data);self.assertEqual(r.status_code,200,r.text);return r.json()['courses']
    self.assertFalse(scan()[0]['cached']);self.assertTrue(scan()[0]['cached']);self.assertEqual(count['read'],1)
    self.assertEqual(scan({'ids':['unselected']}),[]);self.assertEqual(count['read'],1)
    self.assertFalse(scan({'force':True})[0]['cached']);self.assertEqual(count['read'],2)
    FollowupTasks(env['MMVA_ATTENDANCE_DB']).save(source['id'],dict(title='Verify roster',owner=NOEL,due='2026-01-01',status='Open',note='',version=0),{'email':NOEL},[{'email':NOEL}])
    row=scan()[0];self.assertFalse(row['cached']);self.assertEqual(row['openActions'],1);self.assertEqual(count['read'],3)
    state['failed']=True;row=scan({'force':True})[0];self.assertTrue(row['unavailable']);self.assertTrue(row['lastSuccessfulCheckedAt'])
    self.assertTrue(scan()[0]['unavailable']);self.assertEqual(count['read'],5)
