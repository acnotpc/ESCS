import os, tempfile, unittest, sqlite3, copy
from unittest.mock import patch
from fastapi import HTTPException
from mmva_management import ManagementChecks, LABELS
from mmva_trainer_auth import TrainerAccounts
from test_mmva_trainer_picker import PickerTests
from test_mmva_completion import SOURCE

NOEL='noel.pusey@securecareservices.co.uk'
def payload(version=0):
    return dict(code=SOURCE['id'],version=version,leader='Noel Pusey',followup='',checks=[dict(label=l,state='Unverified',reference='',note='') for l in LABELS])

class ManagementTests(unittest.TestCase):
    def test_validation_conflicts_and_persistent_audit(self):
        with tempfile.TemporaryDirectory() as folder:
            path=folder+'/test.sqlite';db=ManagementChecks(path);p=payload()
            p['checks'][0]['state']='Verified'
            with self.assertRaises(HTTPException):db.save('123',p,{'email':NOEL})
            p['checks'][0].update(reference='https://example.invalid/evidence',note='Checked named trainer')
            first=db.save('123',p,{'email':NOEL});self.assertEqual(first['version'],1)
            with self.assertRaises(HTTPException) as conflict:db.save('123',p,{'email':NOEL})
            self.assertEqual(conflict.exception.status_code,409)
            p['version']=1;db.save('123',p,{'email':NOEL})
            self.assertEqual(ManagementChecks(path).latest()['123']['version'],2)
            with sqlite3.connect(path) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM mmva_management_checks').fetchone()[0],2)
            p['version']=2;p['checks'][0]['reference']='javascript:alert(1)'
            with self.assertRaises(HTTPException):db.save('123',p,{'email':NOEL})

    def test_management_requires_enabled_authorised_login(self):
        with tempfile.TemporaryDirectory() as folder:
            env,client=PickerTests().setup_client(folder)
            class Source:
                async def list_courses(self,*args,**kw):
                    self_kw=kw
                    assert self_kw['management'] is True
                    return [dict(SOURCE,status='Booked',url='https://example.invalid/booking')]
                async def resolve(self,*args,**kwargs):return SOURCE
            with patch.dict(os.environ,env),patch('mmva_attendance.MondayCourses',return_value=Source()):
                self.assertEqual(client.get('/attendance/trainer/management',follow_redirects=False).status_code,303)
                self.assertEqual(client.post('/attendance/trainer/management-courses',json={}).status_code,401)
                client.post('/attendance/trainer/login',json={'email':'trainer@example.invalid','password':'test-only-trainer-password'})
                self.assertEqual(client.get('/attendance/trainer/management').status_code,403)
                self.assertEqual(client.post('/attendance/trainer/management-checks',json=payload()).status_code,403)
                for route in ['management-details','management-import','management-allocation-options','management-allocation','management-certificate','management-actions','management-closeout']:
                    self.assertEqual(client.post('/attendance/trainer/'+route,json={}).status_code,403,route)
                accounts=TrainerAccounts(env['MMVA_ATTENDANCE_DB'],env['MMVA_ATTENDANCE_SECRET'])
                invite=accounts.invite(NOEL,'Noel Pusey');accounts.setup(invite['setup_path'].split('token=')[1],'test-noel-only-password','test')
                client.post('/attendance/trainer/login',json={'email':NOEL,'password':'test-noel-only-password'})
                response=client.get('/attendance/trainer/management');self.assertEqual(response.status_code,200);self.assertEqual(response.headers['cache-control'],'no-store')
                self.assertEqual(client.post('/attendance/trainer/management-courses',json={}).status_code,200)
                self.assertEqual(client.post('/attendance/trainer/management-checks',json=payload()).status_code,200)
                self.assertEqual(client.post('/attendance/trainer/management-checks',json=payload()).status_code,409)
                accounts.disable(NOEL)
                self.assertEqual(client.post('/attendance/trainer/management-courses',json={}).status_code,401)
