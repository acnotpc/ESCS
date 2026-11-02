import copy,os,tempfile,unittest
from unittest.mock import patch
from fastapi import HTTPException
from mmva_duplicates import NameReviews,apply_name_reviews,name_revision,NAME_WARNING
from test_mmva_completion import SOURCE,CODE,scans,row,report
import test_mmva_trainer_picker as picker

def sample():
 data=scans();data['registration']['rows']=[row(name='Olamilekan Sule')];data['fitness']['rows']=[row('2',name='Ola Sule')]
 return data
class NameTests(unittest.TestCase):
 def test_confirmation_preserves_names_reopens_on_new_name_and_is_course_scoped(self):
  base=report(SOURCE,sample(),[],'2026-11-02');p=base['delegates'][0];self.assertIn(NAME_WARNING,p['issues'])
  with tempfile.TemporaryDirectory() as folder:
   store=NameReviews(folder+'/db');actor={'email':'trainer@example.invalid'}
   store.save(CODE,p,actor,'Olamilekan Sule','Checked with the delegate',0)
   updated=apply_name_reviews(copy.deepcopy(base),store.history(CODE),'2026-11-02');u=updated['delegates'][0]
   self.assertNotIn(NAME_WARNING,u['issues']);self.assertEqual(u['name'],'Olamilekan Sule');self.assertEqual(u['nameVariants'],p['nameVariants']);self.assertTrue(u['outstanding']);self.assertFalse(u['complete']);self.assertEqual(store.history('OTHER'),{})
   p['nameVariants'].append('Another Name');updated=apply_name_reviews(base,store.history(CODE),'2026-11-02');self.assertIn(NAME_WARNING,updated['delegates'][0]['issues']);self.assertFalse(updated['delegates'][0]['nameReview']['verified'])
   with self.assertRaises(HTTPException):store.save(CODE,p,actor,'Ola Sule','Checked',0)
   with self.assertRaises(HTTPException):store.save(CODE,p,actor,'Not a recorded name','Checked',1)
 def test_route_auth_confirmation_revision_and_saved_verification(self):
  with tempfile.TemporaryDirectory() as folder:
   env,c=picker.PickerTests().setup_client(folder)
   class Source:
    async def resolve(self,*a,**kw):return SOURCE
   class Reader:
    def __init__(self,key):pass
    async def read(self):return sample()
   with patch.dict(os.environ,env),patch('mmva_attendance.MondayCourses',return_value=Source()),patch('mmva_attendance.CompletionReader',Reader):
    url='/attendance/trainer/verify-names';data={'code':CODE,'email':'one@example.invalid','name':'Olamilekan Sule','reason':'Checked with delegate','version':0,'confirm':True}
    self.assertEqual(c.post(url,json=data).status_code,401)
    c.post('/attendance/trainer/login',json={'email':'trainer@example.invalid','password':'test-only-trainer-password'})
    base=c.post('/attendance/trainer/completion',json={'code':CODE}).json();p=base['delegates'][0];self.assertEqual(len(p['nameVariants']),2)
    data['revision']='stale';self.assertEqual(c.post(url,json=data).status_code,409)
    data['revision']=p['nameReview']['revision'];data['confirm']=False;self.assertEqual(c.post(url,json=data).status_code,400)
    data['confirm']=True;data['reason']='';self.assertEqual(c.post(url,json=data).status_code,400)
    data['reason']='Checked with delegate';r=c.post(url,json=data);self.assertEqual(r.status_code,200);p=r.json()['delegates'][0];self.assertNotIn(NAME_WARNING,p['issues']);self.assertTrue(p['nameReview']['verified']);self.assertEqual(p['nameReview']['history'][0]['trainer'],'trainer@example.invalid')
    self.assertEqual(c.post(url,json=data).status_code,409)
