import copy,os,tempfile,unittest
from datetime import datetime
from unittest.mock import patch
from fastapi import HTTPException
from mmva_attendance import Ledger
from mmva_attendance_review import AttendanceReviews,enrich_attendance
from test_mmva_completion import SOURCE,CODE,scans,row,report
import test_mmva_trainer_picker as picker
ACTOR={'email':'trainer@example.invalid','name':'Test Trainer'}
def base():
 data=scans();data['registration']['rows']=[row(),row('2',email='two@example.invalid',name='Another Delegate')]
 attendance=[dict(email='one@example.invalid',name='Test Delegate',day='2026-11-02',step=0,time=1793610000,sync='sent')]
 return report(SOURCE,data,attendance,'2026-11-03')
class AttendanceReviewTests(unittest.TestCase):
 def test_first_scan_triggers_missing_checks_and_future_points_wait(self):
  with tempfile.TemporaryDirectory() as folder:
   path=folder+'/db';Ledger(path,'s');r=base()
   today=enrich_attendance(copy.deepcopy(r),path,'2026-11-02');self.assertEqual(len(today['attendanceReviews']),2);self.assertTrue(any(c['state']=='missing' for c in today['attendanceReviews']))
   past=enrich_attendance(copy.deepcopy(r),path,'2026-11-03');self.assertEqual(len(past['attendanceReviews']),8)
   for p in r['delegates']:p['attendance'][0]['steps']=[None]*4
   empty=enrich_attendance(r,path,'2026-11-03');self.assertEqual(empty['attendanceReviews'],[]);self.assertTrue(all('Attendance' in p['outstanding'] for p in empty['delegates']))
 def test_evidence_is_audited_no_scan_rewrite_and_absence_stays_incomplete(self):
  with tempfile.TemporaryDirectory() as folder:
   path=folder+'/db';Ledger(path,'s');original=base();r=enrich_attendance(copy.deepcopy(original),path,'2026-11-03');c=next(c for c in r['attendanceReviews'] if c['state']=='missing');store=AttendanceReviews(path)
   store.save(CODE,c,'verified','','Confirmed in the classroom register',ACTOR,0)
   r=enrich_attendance(copy.deepcopy(original),path,'2026-11-03');p=next(p for p in r['delegates'] if p['email']==c['email']);point=p['attendance'][0]['steps'][c['step']];self.assertTrue(point['manual']);self.assertIsNone(point['time']);self.assertEqual(point['trainer'],'Test Trainer');self.assertIsNone(original['delegates'][0]['attendance'][0]['steps'][c['step']])
   self.assertEqual(store.history('OTHER'),{})
   with self.assertRaises(HTTPException):store.save(CODE,c,'verified','','Evidence',ACTOR,0)
   store.save(CODE,c,'absent','','Delegate left early',ACTOR,1)
   r=enrich_attendance(copy.deepcopy(original),path,'2026-11-03');p=next(p for p in r['delegates'] if p['email']==c['email']);self.assertIn('Attendance',p['outstanding']);self.assertFalse(p['complete']);self.assertEqual(len(store.history(CODE)[(c['email'],c['day'],c['step'])]),2)
 def test_actual_time_validation_and_changed_scan_reopens_review(self):
  with tempfile.TemporaryDirectory() as folder:
   path=folder+'/db';Ledger(path,'s');original=base();r=enrich_attendance(copy.deepcopy(original),path,'2026-11-03');c=next(c for c in r['attendanceReviews'] if c['state']=='missing');store=AttendanceReviews(path)
   with self.assertRaises(HTTPException):store.save(CODE,c,'verified','bad','Evidence',ACTOR,0)
   with self.assertRaises(HTTPException):store.save(CODE,c,'verified','','',ACTOR,0)
   with self.assertRaises(HTTPException):store.save(CODE,c,'absent','10:00','Evidence',ACTOR,0)
   store.save(CODE,c,'verified','12:00','Trainer verified from register',ACTOR,0,now=1794000000)
   p=next(p for p in original['delegates'] if p['email']==c['email']);p['attendance'][0]['steps'][c['step']]={'time':1793610000,'sync':'sent'}
   changed=enrich_attendance(original,path,'2026-11-03');card=next(x for x in changed['attendanceReviews'] if x['email']==c['email'] and x['step']==c['step']);self.assertTrue(card['needsAction']);self.assertEqual(card['state'],'unverified')
 def test_verified_missing_scans_can_complete_attendance_without_fabricated_times(self):
  with tempfile.TemporaryDirectory() as folder:
   path=folder+'/db';Ledger(path,'s');data=scans()
   for key in ['registration','health','fitness','knowledge','feedback']:data[key]['rows']=[row()]
   raw=report(SOURCE,data,[dict(email='one@example.invalid',name='Test Delegate',day='2026-11-02',step=0,time=1793610000,sync='sent')],'2026-11-03')
   reviewed=enrich_attendance(copy.deepcopy(raw),path,'2026-11-03');store=AttendanceReviews(path)
   for card in reviewed['attendanceReviews']:
    if card['state']=='missing':store.save(CODE,card,'verified','','Signed class register checked',ACTOR,0)
   result=enrich_attendance(copy.deepcopy(raw),path,'2026-11-03');self.assertTrue(result['delegates'][0]['complete']);self.assertEqual(result['attendanceReviewOutstanding'],0)
   self.assertTrue(all(s is None or s['time'] is None for s in result['delegates'][0]['attendance'][0]['steps'][1:]))
 def test_review_history_remains_visible_after_verified_email_link(self):
  from mmva_duplicates import DuplicateLinks
  with tempfile.TemporaryDirectory() as folder:
   path=folder+'/db';Ledger(path,'s');raw=base();r=enrich_attendance(copy.deepcopy(raw),path,'2026-11-03');card=next(c for c in r['attendanceReviews'] if c['state']=='missing');store=AttendanceReviews(path)
   store.save(CODE,card,'verified','','Register checked',ACTOR,0)
   DuplicateLinks(path).save(CODE,card['email'],'corrected@example.invalid',ACTOR,'Verified typo')
   for p in raw['delegates']:
    if p['email']==card['email']:p['email']='corrected@example.invalid'
   result=enrich_attendance(raw,path,'2026-11-03');c=next(c for c in result['attendanceReviews'] if c['email']=='corrected@example.invalid' and c['step']==card['step']);self.assertTrue(c['needsAction']);self.assertEqual(c['version'],0);self.assertEqual(c['history'][0]['email'],card['email'])
   store.save(CODE,c,'verified','','Evidence checked against corrected identity',ACTOR,0)
 def test_endpoint_requires_trainer_consent_and_current_snapshot(self):
  with tempfile.TemporaryDirectory() as folder:
   env,client=picker.PickerTests().setup_client(folder)
   class Source:
    async def resolve(self,*a,**kw):return SOURCE
   class Reader:
    def __init__(self,key):pass
    async def read(self):return scans()
   from mmva_completion import report as real_report
   def fake_report(*args):return base()
   with patch.dict(os.environ,env),patch('mmva_attendance.MondayCourses',return_value=Source()),patch('mmva_attendance.CompletionReader',Reader),patch('mmva_attendance.completion_report',side_effect=fake_report),patch('mmva_attendance.london_day',return_value='2026-11-03'):
    url='/attendance/trainer/attendance-review';data={'code':CODE,'confirm':True,'state':'verified','actualTime':'','reason':'Checked classroom register','version':0}
    self.assertEqual(client.post(url,json=data).status_code,401)
    client.post('/attendance/trainer/login',json={'email':ACTOR['email'],'password':'test-only-trainer-password'})
    r=client.post('/attendance/trainer/completion',json={'code':CODE}).json();c=next(c for c in r['attendanceReviews'] if c['state']=='missing');data.update({k:c[k] for k in ['email','day','step','revision','version']})
    data['revision']='stale';self.assertEqual(client.post(url,json=data).status_code,409)
    data['revision']=c['revision'];data['confirm']=False;self.assertEqual(client.post(url,json=data).status_code,400)
    data['confirm']=True;r=client.post(url,json=data);self.assertEqual(r.status_code,200);card=next(x for x in r.json()['attendanceReviews'] if x['email']==c['email'] and x['step']==c['step']);self.assertEqual(card['state'],'verified');self.assertEqual(card['history'][0]['actor'],ACTOR['email'])
    self.assertEqual(client.post(url,json=data).status_code,409)
