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
 def test_two_minutes_or_hours_cannot_unlock_lunch(self):
  with tempfile.TemporaryDirectory() as folder:
   ledger,person,now=self.setup_ledger(folder);ledger.record(person,0,now)
   for delay in [120,7200]:
    with self.assertRaises(HTTPException) as e:ledger.record(person,1,now+delay)
    self.assertIn('trainer to open',e.exception.detail)
   change(ledger,SOURCE['id'],london_day(now),1,0,{'name':'Trainer','email':'trainer@example.invalid'})
   ledger.record(person,1,now+7200)
   with self.assertRaises(HTTPException):ledger.record(person,2,now+7300)
 def test_gate_rechecked_and_missing_previous_point_requires_trainer(self):
  with tempfile.TemporaryDirectory() as folder:
   ledger,person,now=self.setup_ledger(folder);self.assertTrue(ledger.status(person,now)['pointOpen'])
   change(ledger,SOURCE['id'],london_day(now),1,0,{'name':'Trainer','email':'trainer@example.invalid'})
   self.assertFalse(ledger.status(person,now)['pointOpen'])
   with self.assertRaises(HTTPException):ledger.record(person,0,now)
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
 def test_protected_control_route_and_explicit_confirmation(self):
  with tempfile.TemporaryDirectory() as folder:
   env,c=picker.PickerTests().setup_client(folder)
   class Reader:
    async def resolve(self,*args,**kwargs):return SOURCE
   with patch.dict('os.environ',env),patch('mmva_attendance.MondayCourses',return_value=Reader()):
    self.assertEqual(c.post('/attendance/trainer/attendance-point',json={'code':SOURCE['id']}).status_code,401)
    c.post('/attendance/trainer/login',json={'email':'trainer@example.invalid','password':'test-only-trainer-password'})
    initial=c.post('/attendance/trainer/attendance-point',json={'code':SOURCE['id']});self.assertEqual(initial.status_code,200);self.assertEqual(initial.json()['stage'],0)
    data={'code':SOURCE['id'],'stage':1,'version':0}
    self.assertEqual(c.post('/attendance/trainer/attendance-point',json=data).status_code,400)
    data['confirm']=True;self.assertEqual(c.post('/attendance/trainer/attendance-point',json=data).status_code,200)
    self.assertEqual(c.post('/attendance/trainer/attendance-point',json=data).status_code,409)
