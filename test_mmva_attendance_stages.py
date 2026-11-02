import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from mmva_attendance import Ledger,london_day
from mmva_attendance_stages import current,change
from test_mmva_completion import SOURCE
import test_mmva_trainer_picker as picker

class StageTests(unittest.TestCase):
 def setup_ledger(self,folder):
  ledger=Ledger(str(Path(folder)/'ledger.sqlite'),'x'*40);now=1793610780
  source=dict(SOURCE,day=london_day(now),trainer='Trainer');code=ledger.monday_course(source)
  person=ledger.enrol({'code':code,'first':'Test','last':'Delegate','email':'test@example.invalid'},now)
  return ledger,person,now
 def test_each_point_requires_thirty_minutes_and_exact_boundary_is_allowed(self):
  with tempfile.TemporaryDirectory() as folder:
   ledger,person,now=self.setup_ledger(folder);ledger.record(person,0,now)
   for step in range(1,4):
    previous=now+(step-1)*1800
    for delay in [0,120,1799,1799.999]:
     self.assertFalse(ledger.status(person,previous+delay)['pointOpen'])
     with self.assertRaises(HTTPException):ledger.record(person,step,previous+delay)
    self.assertTrue(ledger.status(person,previous+1800)['pointOpen']);ledger.record(person,step,previous+1800)
   self.assertIsNone(ledger.status(person,now+5400)['next']);self.assertFalse(ledger.status(person,now+5400)['pointOpen'])
 def test_legacy_closed_stage_does_not_control_attendance_and_restart_preserves_timer(self):
  with tempfile.TemporaryDirectory() as folder:
   ledger,person,now=self.setup_ledger(folder);change(ledger,SOURCE['id'],london_day(now),4,0,{'name':'Trainer','email':'trainer@example.invalid'})
   self.assertTrue(ledger.status(person,now)['pointOpen']);ledger.record(person,0,now)
   restarted=Ledger(ledger.path,'x'*40);self.assertEqual(restarted.status(person,now+120)['waitSeconds'],1680)
   with self.assertRaises(HTTPException):restarted.record(person,2,now+1800)
   restarted.record(person,1,now+1800)
 def test_restart_day_isolation_audit_and_conflicting_trainer(self):
  with tempfile.TemporaryDirectory() as folder:
   ledger,person,now=self.setup_ledger(folder);day=london_day(now);actor={'name':'Trainer','email':'trainer@example.invalid'}
   change(ledger,SOURCE['id'],day,2,0,actor)
   with self.assertRaises(HTTPException):change(ledger,SOURCE['id'],day,3,0,actor)
   with self.assertRaises(HTTPException):change(ledger,SOURCE['id'],day,0,1,actor)
   change(ledger,SOURCE['id'],day,0,1,actor,'Reviewed a missed morning entry with the delegate')
   other=Ledger(ledger.path,'x'*40)
   with other.db() as db:
    self.assertEqual(current(db,SOURCE['id'],day)['version'],2)
    self.assertEqual(current(db,SOURCE['id'],london_day(now+86400))['stage'],0)
    self.assertEqual(current(db,'999',day)['stage'],0)
    self.assertEqual(db.execute('SELECT COUNT(*) FROM mmva_attendance_stage_audit').fetchone()[0],2)
 def test_old_control_route_is_retired(self):
  with tempfile.TemporaryDirectory() as folder:
   env,c=picker.PickerTests().setup_client(folder)
   with patch.dict('os.environ',env):
    self.assertEqual(c.post('/attendance/trainer/attendance-point',json={'code':SOURCE['id']}).status_code,401)
    c.post('/attendance/trainer/login',json={'email':'trainer@example.invalid','password':'test-only-trainer-password'})
    self.assertEqual(c.post('/attendance/trainer/attendance-point',json={'code':SOURCE['id'],'stage':4}).status_code,410)
    r=c.get('/attendance/trainer/points',follow_redirects=False);self.assertEqual(r.status_code,303);self.assertEqual(r.headers['location'],'/attendance/trainer/review')
