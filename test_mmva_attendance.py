import asyncio
import concurrent.futures
import tempfile
import unittest
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from unittest.mock import patch
from mmva_attendance import Ledger, london_day, build_attendance_router

class AttendanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'attendance.sqlite3')
        self.db = Ledger(self.path, 'x' * 40)
        self.now = 1793642400
        with self.db.db() as conn:
            conn.execute('INSERT INTO course(code,title,day,trainer) VALUES(?,?,?,?)', ('COURSE01', 'MMVA course', london_day(self.now), 'Trainer'))
        self.person = self.db.enrol({'code':'COURSE01','first':'Test','last':'Delegate','email':'test@example.org','organisation':'Test org'}, self.now)

    def tearDown(self):
        self.temp.cleanup()

    def test_order_duplicates_and_completion(self):
        with self.assertRaises(HTTPException): self.db.record(self.person, 2, self.now)
        for step in range(4):
            self.db.record(self.person, step, self.now + step*3600)
            with self.assertRaises(HTTPException): self.db.record(self.person, step, self.now + step*3600)
        self.assertIsNone(self.db.status(self.person, self.now + 4*3600)['next'])

    def test_identity_canonicalisation_and_restart(self):
        other = self.db.enrol({'code':'course01','first':'Other','last':'Name','email':' TEST@example.org ','organisation':'Org'}, self.now)
        self.assertEqual(other,self.person)
        self.db.record(other,0,self.now)
        restarted = Ledger(self.path,'x'*40)
        self.assertEqual(restarted.status(other,self.now)['step'],1)

    def test_concurrent_double_click(self):
        def submit(_):
            try: return self.db.record(self.person,0,self.now)
            except HTTPException: return None
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results = list(pool.map(submit,range(4)))
        self.assertEqual(sum(x is not None for x in results),1)

    def test_new_day_requires_new_course(self):
        with self.assertRaises(HTTPException): self.db.record(self.person,0,self.now+86400)
        with self.db.db() as conn:
            conn.execute('INSERT INTO course(code,title,day,trainer) VALUES(?,?,?,?)', ('COURSE02','MMVA course',london_day(self.now+86400),'Trainer'))
        person = self.db.enrol({'code':'COURSE02','first':'Test','last':'Delegate','email':'test@example.org','organisation':'Org'},self.now+86400)
        self.assertEqual(self.db.status(person,self.now+86400)['step'],0)

    def test_cooldown(self):
        self.db.record(self.person,0,self.now)
        with self.assertRaises(HTTPException): self.db.record(self.person,1,self.now+2)

    def test_uncertain_delivery_not_retried(self):
        event=self.db.record(self.person,0,self.now);calls=[]
        def fail(req):
            calls.append(req)
            raise httpx.ReadTimeout('timeout')
        transport=httpx.MockTransport(fail)
        asyncio.run(self.db.deliver(event,'secret',transport))
        asyncio.run(self.db.deliver(event,'secret',transport))
        self.assertEqual(len(calls),1)
        with self.db.db() as conn:
            self.assertEqual(conn.execute('SELECT state FROM event').fetchone()[0],'uncertain')

    def test_success_and_mapping(self):
        event=self.db.record(self.person,0,self.now)
        def ok(req):
            self.assertEqual(req.headers['APIKEY'],'test-secret')
            self.assertIn(b'submission%5B8%5D=1.+Morning+sign-in',req.content)
            self.assertNotIn('test-secret',str(req.url))
            return httpx.Response(200,json={'responseCode':200,'content':{'submissionID':'12345'}})
        asyncio.run(self.db.deliver(event,'test-secret',httpx.MockTransport(ok)))
        with self.db.db() as conn:
            self.assertEqual(conn.execute('SELECT state FROM event').fetchone()[0],'sent')

    def client(self):
        app=FastAPI()
        def auth(): raise HTTPException(401,'Not authorised')
        app.include_router(build_attendance_router(auth))
        return TestClient(app,base_url='https://testserver')

    def test_disabled_fails_closed(self):
        with patch.dict('os.environ',{},clear=True):
            c=self.client()
            self.assertEqual(c.get('/attendance/status').status_code,503)
            self.assertEqual(c.post('/attendance/admin/courses',json={}).status_code,401)

    def test_routes_tampering_and_replay(self):
        env={'MMVA_ATTENDANCE_ENABLED':'true','MMVA_ATTENDANCE_DESTINATION_VERIFIED':'true','MMVA_ATTENDANCE_SECRET':'x'*40,'MMVA_ATTENDANCE_DB':self.path,'MMVA_ATTENDANCE_JOTFORM_API_KEY':'test'}
        async def delivered(*args,**kwargs): pass
        with patch.dict('os.environ',env),patch('mmva_attendance.time.time',return_value=self.now),patch.object(Ledger,'deliver',delivered):
            c=self.client()
            self.assertEqual(c.post('/attendance/register',data={}).status_code,415)
            s=c.post('/attendance/register',json={'code':'COURSE01','first':'Test','last':'Delegate','email':'test@example.org','organisation':'Org'})
            self.assertEqual(s.status_code,200)
            self.assertIn('HttpOnly',s.headers['set-cookie'])
            token=s.json()['token']
            self.assertEqual(c.post('/attendance/record',json={'token':token+'broken','confirm':True}).status_code,409)
            self.assertEqual(c.post('/attendance/record',json={'token':token,'confirm':False}).status_code,400)
            self.assertEqual(c.post('/attendance/record',json={'token':token,'confirm':True}).status_code,200)
            self.assertEqual(c.post('/attendance/record',json={'token':token,'confirm':True}).status_code,409)
            self.assertEqual(c.get('/attendance/status').json()['step'],1)

if __name__=='__main__': unittest.main()
