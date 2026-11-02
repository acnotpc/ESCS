import copy,os,tempfile,unittest
from unittest.mock import patch
from fastapi import HTTPException
from mmva_duplicates import DuplicateLinks,candidates,canonical,linked_rows
from test_mmva_completion import SOURCE,CODE,scans,row,report
import test_mmva_trainer_picker as picker

def sample():
 data=scans();data['registration']['rows']=[row(email='adam@example.com',name='Adam Bull')];data['fitness']['rows']=[row('2',email='adam@example.con',name='Adam Bull')]
 return data
class DuplicateTests(unittest.TestCase):
 def test_suggestions_require_matching_full_name_and_close_email(self):
  r=report(SOURCE,sample(),[],'2026-11-02');self.assertEqual(len(candidates(r)),1)
  r['delegates'][0]['name']='Other Person';self.assertEqual(candidates(r),[])
  r['delegates'][0]['name']='Adam Bull';r['delegates'][0]['email']='unrelated@example.org';self.assertEqual(candidates(r),[])
 def test_course_scoped_persistent_alias_and_unchanged_source(self):
  with tempfile.TemporaryDirectory() as folder:
   store=DuplicateLinks(folder+'/db');actor={'email':'trainer@example.invalid'}
   store.save(CODE,'adam@example.con','adam@example.com',actor,'Verified directly with the delegate')
   rows=sample()['fitness']['rows'];result=linked_rows(rows,CODE,store.links(CODE));self.assertEqual(result[0]['email'],'adam@example.com');self.assertEqual(rows[0]['email'],'adam@example.con')
   self.assertEqual(store.links('OTHER'),{});self.assertEqual(store.history(CODE)[0]['trainer'],actor['email'])
   with self.assertRaises(HTTPException):store.save(CODE,'adam@example.com','adam@example.con',actor,'reverse')
   with self.assertRaises(HTTPException):store.save(CODE,'another@example.com','adam@example.com',actor,'')
 def test_duplicate_attendance_is_flagged_without_changing_times(self):
  rows=[dict(email='adam@example.com',name='Adam Bull',day='2026-11-02',step=0,time=t,sync='sent') for t in [100,200]]
  p=report(SOURCE,sample(),rows,'2026-11-02')['delegates'][0];self.assertTrue(any('Multiple attendance' in x for x in p['issues']));self.assertEqual(p['attendance'][0]['steps'][0]['time'],100)
 def test_protected_route_rechecks_revision_and_combines_report(self):
  with tempfile.TemporaryDirectory() as folder:
   env,c=picker.PickerTests().setup_client(folder)
   class Source:
    async def resolve(self,*a,**kw):return SOURCE
   class Reader:
    def __init__(self,key):pass
    async def read(self):return sample()
   with patch.dict(os.environ,env),patch('mmva_attendance.MondayCourses',return_value=Source()),patch('mmva_attendance.CompletionReader',Reader):
    url='/attendance/trainer/combine-delegates';data={'code':CODE,'source':'adam@example.con','target':'adam@example.com','confirm':True,'reason':'Confirmed the email with Adam'}
    self.assertEqual(c.post(url,json=data).status_code,401)
    c.post('/attendance/trainer/login',json={'email':'trainer@example.invalid','password':'test-only-trainer-password'})
    base=c.post('/attendance/trainer/completion',json={'code':CODE}).json();self.assertEqual(len(base['possibleDuplicates']),1)
    data['revision']='stale';self.assertEqual(c.post(url,json=data).status_code,409)
    data['revision']=base['possibleDuplicates'][0]['revision'];data['confirm']=False;self.assertEqual(c.post(url,json=data).status_code,400)
    data['confirm']=True;merged=c.post(url,json=data);self.assertEqual(merged.status_code,200);merged=merged.json();self.assertEqual(len(merged['delegates']),1);self.assertEqual(merged['possibleDuplicates'],[]);p=merged['delegates'][0];self.assertEqual(p['email'],'adam@example.com');self.assertEqual(p['forms']['fitness']['entries'][0]['id'],'2');self.assertEqual(len(merged['identityLinks']),1)
    self.assertEqual(c.post(url,json=data).status_code,409)
