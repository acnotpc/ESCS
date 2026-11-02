import asyncio,copy,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from fastapi import HTTPException
from mmva_health_followup import declaration_flag,HealthFollowups,enrich
from mmva_completion import project,SPECS,CompletionReader
from test_mmva_completion import SOURCE,scans,row,report
import test_mmva_trainer_picker as picker

def answers(**values):return {k:{'answer':v} for k,v in values.items()}
def flagged(values=None):return declaration_flag(values or answers(**{'80':'Adjustments to be discussed','78':'No'}),'test-key')
def base_report():
 data=scans();r=row();r['health']=flagged();data['health']['rows']=[r]
 return report(SOURCE,data,[],'2026-11-02')
class HealthTests(unittest.TestCase):
 def test_current_positive_negative_discussion_and_unknown(self):
  self.assertEqual(declaration_flag(answers(**{'80':'No adjustments required','78':'No'}),'k')['state'],'clear')
  for a in [answers(**{'80':'Adjustments to be discussed','78':'No'}),answers(**{'80':'No adjustments required','78':'Yes'}),answers(**{'76':['None relevant','Diabetes']}),answers(**{'66':'PRIVATE MEDICAL TEXT'})]:
   f=declaration_flag(a,'k');self.assertEqual(f['state'],'attention');self.assertNotIn('Diabetes',json.dumps(f));self.assertNotIn('PRIVATE MEDICAL',json.dumps(f))
  self.assertEqual(declaration_flag({},'k')['state'],'unknown')
  self.assertEqual(declaration_flag(answers(**{'78':'No'}),'k')['state'],'unknown')
  self.assertEqual(declaration_flag(answers(**{'51':'None of the above'}),'k')['state'],'clear')
 def test_health_projection_never_returns_details_and_revision_changes(self):
  a=answers(**{'81':SOURCE['id'],'11':'test@example.invalid','14':{'first':'Test','last':'Delegate'},'80':'Adjustments to be discussed','78':'Yes','77':'PRIVATE DETAILS'})
  raw={'id':'99','status':'ACTIVE','answers':a};r=project(raw,SPECS['health'],'k');self.assertNotIn('PRIVATE DETAILS',json.dumps(r));self.assertEqual(r['health']['state'],'attention')
  old=r['health']['revision'];a['77']['answer']='CHANGED PRIVATE DETAILS';self.assertNotEqual(project(raw,SPECS['health'],'k')['health']['revision'],old)
 def test_history_restart_conflict_and_reopened_declaration(self):
  with tempfile.TemporaryDirectory() as folder:
   path=str(Path(folder)/'notes.sqlite');db=HealthFollowups(path);actor={'name':'Trainer One','email':'trainer@example.invalid'};base=base_report();rev=base['delegates'][0]['forms']['health']['entries'][0]['health']['revision']
   self.assertEqual(db.save(SOURCE['id'],'1',rev,'Agreed an adjustment','resolved',actor,0),1)
   with self.assertRaises(HTTPException) as conflict:db.save(SOURCE['id'],'1',rev,'Another note','open',actor,0)
   self.assertEqual(conflict.exception.status_code,409)
   enriched=enrich(copy.deepcopy(base),HealthFollowups(path).notes(SOURCE['id']));self.assertFalse(enriched['healthFollowups'][0]['outstanding']);self.assertEqual(enriched['healthFollowups'][0]['followup']['history'][0]['trainer'],'Trainer One')
   base['delegates'][0]['forms']['health']['entries'][0]['health']['revision']='f'*64
   base['delegates'][0]['forms']['health']['entries'][0]['health']['state']='clear'
   changed=enrich(copy.deepcopy(base),db.notes(SOURCE['id']));self.assertTrue(changed['healthFollowups'][0]['outstanding']);self.assertTrue(changed['healthFollowups'][0]['followup']['changed'])
   db.save(SOURCE['id'],'1','f'*64,'Further discussion arranged','in_progress',actor,1);self.assertEqual(len(db.notes(SOURCE['id'])['1']),2);self.assertEqual(db.notes('999'),{})
 def test_empty_comment_and_invalid_status_rejected(self):
  with tempfile.TemporaryDirectory() as folder:
   db=HealthFollowups(str(Path(folder)/'notes'));actor={'name':'Trainer','email':'t@example.invalid'}
   for note,state in [(' ','resolved'),('x'*2001,'open'),('Comment','cleared')]:
    with self.assertRaises(HTTPException):db.save(SOURCE['id'],'1','a'*64,note,state,actor,0)
 def test_save_route_auth_exact_course_and_fresh_revision(self):
  with tempfile.TemporaryDirectory() as folder:
   env,c=picker.PickerTests().setup_client(folder)
   class Source:
    async def resolve(self,*args,**kwargs):return SOURCE
   declaration={'course':SOURCE['id'],'health':flagged()}
   class Reader:
    def __init__(self,key):pass
    async def read_health_submission(self,sid):return declaration
   data={'code':SOURCE['id'],'submission':'1','revision':declaration['health']['revision'],'version':0,'note':'Discussion and agreed adjustment recorded.','state':'resolved'}
   with patch.dict(os.environ,env),patch('mmva_attendance.MondayCourses',return_value=Source()),patch('mmva_attendance.CompletionReader',Reader):
    self.assertEqual(c.post('/attendance/trainer/health-followup',json=data).status_code,401)
    c.post('/attendance/trainer/login',json={'email':'trainer@example.invalid','password':'test-only-trainer-password'})
    self.assertEqual(c.post('/attendance/trainer/health-followup',json=data).status_code,200)
    self.assertEqual(c.post('/attendance/trainer/health-followup',json=data).status_code,409)
    declaration['course']='999';self.assertEqual(c.post('/attendance/trainer/health-followup',json=data).status_code,400)
    declaration['course']=SOURCE['id'];data['revision']='b'*64;self.assertEqual(c.post('/attendance/trainer/health-followup',json=data).status_code,409)
 def test_single_submission_read_checks_form_and_minimises(self):
  def handler(req):
   return httpx.Response(200,json={'responseCode':200,'content':{'id':'123','form_id':SPECS['health'][0],'status':'ACTIVE','answers':answers(**{'81':SOURCE['id'],'80':'Adjustments to be discussed','66':'PRIVATE'})}})
  r=asyncio.run(CompletionReader('test',httpx.MockTransport(handler)).read_health_submission('123'));self.assertEqual(r['course'],SOURCE['id']);self.assertNotIn('PRIVATE',json.dumps(r))
