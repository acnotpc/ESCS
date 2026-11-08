import asyncio,copy,json,os,sqlite3,tempfile,unittest
from unittest.mock import patch
import httpx
from fastapi import HTTPException
from mmva_management_records import ManagementRecords,closeout_blockers
from mmva_monday_management import MondayManagement,allocation_revision
from mmva_management import LABELS
from test_mmva_management import NOEL
from test_mmva_completion import SOURCE

class RecordTests(unittest.TestCase):
 def test_import_idempotency_preserves_original_without_overwrite(self):
  with tempfile.TemporaryDirectory() as d:
   records=ManagementRecords(d+'/db');record=dict(id='123',monday='https://exclusive-gang.monday.com/boards/4727626523/pulses/123',notes='Original long note',checks=[{'state':'Verified'}])
   self.assertEqual(records.import_legacy([record],{'email':NOEL})['imported'],1)
   self.assertEqual(records.import_legacy([record],{'email':NOEL})['duplicates'],1)
   self.assertEqual(records.legacy('123')[0]['record'],record)
   with self.assertRaises(HTTPException):records.import_legacy([dict(record,id='456')],{'email':NOEL})
 def test_certificate_validation_versions_and_void_history(self):
  with tempfile.TemporaryDirectory() as d:
   records=ManagementRecords(d+'/db');source=dict(SOURCE,end='2026-01-01');people=[dict(email='one@example.invalid',name='Learner One')]
   data=dict(email='one@example.invalid',outcome='Pass',reason='Checked assessment evidence',version=0,confirm=True,assessmentEvidence='https://example.invalid/assessment',issued='2026-01-01',expires='2027-01-01',number='MMVA-123',certificateLink='https://example.invalid/cert')
   self.assertEqual(records.save_certificate(source,data,{'email':NOEL},people)['version'],1)
   with self.assertRaises(HTTPException) as exc:records.save_certificate(source,data,{'email':NOEL},people)
   self.assertEqual(exc.exception.status_code,409)
   records.save_certificate(source,dict(data,version=1,outcome='Voided',reason='Incorrect certificate reference'),{'email':NOEL},people)
   self.assertEqual(records.certificates(source['id'])[0]['outcome'],'Voided');self.assertEqual(len(records.certificate_history(source['id'])),2)
   for change in [dict(confirm=False),dict(email='unknown@example.invalid'),dict(assessmentEvidence='javascript:alert(1)'),dict(issued='2020-01-01'),dict(expires='2025-01-01')]:
    with self.assertRaises(HTTPException):records.save_certificate(source,dict(data,version=2,**change),{'email':NOEL},people)
 def test_closeout_blocks_unknown_roster_sources_and_unresolved_records(self):
  source=dict(SOURCE,end='2026-01-01');report=dict(delegates=[dict(email='one@example.invalid',complete=True)],courseForms={'venue':{'available':True,'entries':[{'id':'1'}]}},unmatched=[],sourceErrors=[])
  management=dict(leader='Noel',followup='',checks=[dict(label=l,state='Verified') for l in LABELS]);certs=[dict(email='one@example.invalid',outcome='Pass',number='TEST',certificateLink='https://example.invalid/cert',issued='2026-01-01',expires='2027-01-01')]
  self.assertEqual(closeout_blockers(source,report,management,certs,1,'2026-01-02'),[])
  for changed in [dict(sourceErrors=['registration']),dict(unmatched=[{'id':'2'}]),dict(delegates=[dict(email='one@example.invalid',complete=False)]),dict(healthFollowupOutstanding=1),dict(courseForms={})]:self.assertTrue(closeout_blockers(source,dict(report,**changed),management,certs,1,'2026-01-02'))
  self.assertTrue(closeout_blockers(source,report,management,certs,2,'2026-01-02'));self.assertTrue(closeout_blockers(source,report,management,[],1,'2026-01-02'))

class MondayWriteTests(unittest.TestCase):
 def test_allocation_only_writes_exact_matched_enabled_account_columns(self):
  calls=[]
  def handler(req):
   data=json.loads(req.content);calls.append(data)
   if 'mutation' not in data['query']:return httpx.Response(200,json={'data':{'boards':[{'id':'4727626523','columns':[{'id':'dropdown4','type':'dropdown','settings_str':json.dumps({'labels':[{'id':2,'name':'Andrew Challoner'}]})},{'id':'long_text_mm8qrg2b','type':'long_text'},{'id':'status','type':'status'}]}]}})
   values=json.loads(data['variables']['value']);self.assertEqual(set(values),{'dropdown4','long_text_mm8qrg2b'});self.assertNotIn('status',values)
   return httpx.Response(200,json={'data':{'change_multiple_column_values':{'id':'123'}}})
  client=MondayManagement('test',httpx.MockTransport(handler));accounts=[dict(name='Andy Challoner',email='andy@securecareservices.co.uk')]
  self.assertEqual(asyncio.run(client.allocate('123',[dict(label=2,email='andy@securecareservices.co.uk')],accounts))['emails'],['andy@securecareservices.co.uk'])
  before=len([c for c in calls if 'mutation' in c['query']])
  with self.assertRaises(HTTPException):asyncio.run(client.allocate('123',[dict(label=2,email=NOEL)],accounts))
  self.assertEqual(len([c for c in calls if 'mutation' in c['query']]),before)
 def test_provider_failure_is_unconfirmed_and_has_no_retry(self):
  calls=[]
  def handler(req):calls.append(req);return httpx.Response(503,json={'errors':[{'message':'private provider message'}]})
  with self.assertRaises(HTTPException) as exc:asyncio.run(MondayManagement('test',httpx.MockTransport(handler)).write('123',{'status':{'label':'Course Complete'}}))
  self.assertEqual(len(calls),1);self.assertNotIn('private provider',exc.exception.detail)

class CloseoutRouteTests(unittest.TestCase):
 def test_closeout_requires_fresh_manager_preview_and_explicit_roster_confirmation(self):
  from test_mmva_trainer_picker import PickerTests
  from mmva_trainer_auth import TrainerAccounts
  from mmva_management import ManagementChecks
  from test_mmva_management import payload
  from unittest.mock import AsyncMock
  with tempfile.TemporaryDirectory() as folder:
   env,c=PickerTests().setup_client(folder);accounts=TrainerAccounts(env['MMVA_ATTENDANCE_DB'],env['MMVA_ATTENDANCE_SECRET']);invite=accounts.invite(NOEL,'Noel Pusey');accounts.setup(invite['setup_path'].split('token=')[1],'noel-test-only-password','test')
   source=dict(SOURCE,end='2026-01-01',start='2026-01-01',status='Booked');state={'value':'Booked'}
   class Reader:
    token='test'
    async def resolve(self,*args,**kwargs):return dict(source,status=state['value'])
   scan={};person=dict(name='One',email='one@example.invalid',complete=True,forms={'health':{'entries':[]},'fitness':{'entries':[]}})
   base=dict(delegates=[person],unmatched=[],sourceErrors=[],courseForms={'venue':{'available':True,'entries':[{'id':'1'}]}})
   class Completion:
    def __init__(self,*args):pass
    async def read(self):return scan
   with patch.dict(os.environ,env),patch('mmva_attendance.MondayCourses',return_value=Reader()),patch('mmva_attendance.CompletionReader',Completion),patch('mmva_attendance.completion_report',return_value=copy.deepcopy(base)),patch('mmva_attendance.apply_name_reviews',side_effect=lambda r,*a:r),patch('mmva_attendance.enrich_attendance',side_effect=lambda r,*a:r),patch('mmva_attendance.feedback_status',return_value={}),patch('mmva_attendance.MondayManagement') as writer:
    c.post('/attendance/trainer/login',json={'email':NOEL,'password':'noel-test-only-password'})
    checks=payload();checks['checks']=[dict(label=l,state='Verified',reference='https://example.invalid/evidence',note='Checked') for l in LABELS];ManagementChecks(env['MMVA_ATTENDANCE_DB']).save(source['id'],checks,{'email':NOEL})
    records=ManagementRecords(env['MMVA_ATTENDANCE_DB']);records.save_certificate(source,dict(email=person['email'],outcome='Refer',version=0,assessmentEvidence='https://example.invalid/assessment',reason='Further assessment needed; referral recorded',confirm=True),{'email':NOEL},[person])
    # Preview never changes Monday, and absent preview cannot commit.
    r=c.post('/attendance/trainer/management-closeout',json=dict(code=source['id'],expected=1));self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['blockers'],[],r.text);token=r.json()['preview'];writer.return_value.write.assert_not_called()
    writer.return_value.columns=AsyncMock(return_value=[])
    async def write(*args):state['value']='Course Complete'
    writer.return_value.write=AsyncMock(side_effect=write)
    common=dict(code=source['id'],expected=1,preview=token,commit=True,confirm=True,reason='Reviewed full roster and outcomes')
    self.assertEqual(c.post('/attendance/trainer/management-closeout',json=common).status_code,400)
    success=c.post('/attendance/trainer/management-closeout',json=dict(common,rosterConfirmed=True));self.assertEqual(success.status_code,200,success.text);self.assertTrue(success.json()['closed']);writer.return_value.write.assert_awaited_once()
    self.assertEqual(c.post('/attendance/trainer/management-closeout',json=dict(common,rosterConfirmed=True)).status_code,409)
