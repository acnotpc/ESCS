import asyncio,json,tempfile,unittest,copy
import httpx
from fastapi import HTTPException
from mmva_management_tools import ExpectedRoster
from mmva_management_sync import ManagementSync,summary_text
from mmva_completion import report
from test_mmva_completion import SOURCE,scans,row
from mmva_feedback_summary import feedback_status,roster_hash

class RosterSyncTests(unittest.TestCase):
 def test_expected_people_without_any_records_are_flagged_not_completed(self):
  expected=[dict(name='Expected Learner',email='expected@example.invalid')]
  r=report(SOURCE,scans(),[],'2026-11-08',expected=expected);p=r['delegates'][0]
  self.assertEqual(p['email'],expected[0]['email']);self.assertFalse(p['complete']);self.assertIn('Attendance',p['outstanding']);self.assertEqual(p['forms']['feedback']['state'],'missing');self.assertTrue(r['expectedRosterRecorded'])
  unavailable=scans();unavailable['feedback']['available']=False
  r=report(SOURCE,unavailable,[],'2026-11-08',expected=expected);self.assertEqual(r['delegates'][0]['forms']['feedback']['state'],'unavailable')
 def test_expected_list_prevents_feedback_false_completion(self):
  data=scans()
  for key in ['registration','health','fitness','knowledge','feedback']:data[key]['rows']=[row()]
  r=report(SOURCE,data,[],'2026-11-08');confirmation={'roster_hash':roster_hash(r),'version':1,'recorded_at':'test'}
  enlarged=report(SOURCE,data,[],'2026-11-08',expected=[dict(name='No responses',email='no-responses@example.invalid')])
  state=feedback_status(enlarged,confirmation);self.assertFalse(state['ready']);self.assertFalse(state['confirmed']);self.assertEqual(state['expected'],2)
 def test_roster_validation_concurrency_and_history(self):
  with tempfile.TemporaryDirectory() as f:
   r=ExpectedRoster(f+'/db');actor={'email':'manager@example.invalid'};p=dict(people=[dict(name='Full Name',email=' PERSON@example.invalid ')],version=0,confirm=True,reason='Verified booking list')
   first=r.save('123',p,actor);self.assertEqual(first['people'][0]['email'],'person@example.invalid')
   with self.assertRaises(HTTPException) as e:r.save('123',p,actor)
   self.assertEqual(e.exception.status_code,409)
   for invalid in [dict(confirm=False),dict(reason=''),dict(people=[dict(name='',email='x@example.invalid')]),dict(people=p['people']*2),dict(people=[dict(name='Name',email='invalid')])]:
    with self.assertRaises(HTTPException):r.save('123',dict(p,version=1,**invalid),actor)
   r.save('123',dict(p,version=1,reason='Updated verified source'),actor)
   self.assertEqual(r.latest('123')['version'],2)
 def test_sync_is_append_only_escaped_idempotent_and_no_blind_retry(self):
  with tempfile.TemporaryDirectory() as f:
   store=ManagementSync(f+'/db');actor={'email':'manager@example.invalid'};q=store.queue('123','<script>not HTML</script>',actor);calls=[]
   def handler(req):
    data=json.loads(req.content);calls.append(data);self.assertIn('create_update',data['query']);self.assertEqual(data['variables']['item'],'123');self.assertIn('&lt;script&gt;',data['variables']['body'])
    return httpx.Response(200,json={'data':{'create_update':{'id':'456','body':data['variables']['body']}}})
   result=asyncio.run(store.send('123',q['digest'],'test',httpx.MockTransport(handler)));self.assertEqual(result['state'],'confirmed')
   self.assertEqual(store.queue('123','<script>not HTML</script>',actor)['state'],'confirmed')
   asyncio.run(store.send('123',q['digest'],'test',httpx.MockTransport(handler)));self.assertEqual(len(calls),1)
   other=store.queue('123','New evidence',actor)
   def failed(req):calls.append(req);return httpx.Response(503,json={})
   result=asyncio.run(store.send('123',other['digest'],'test',httpx.MockTransport(failed)));self.assertEqual(result['state'],'uncertain')
   asyncio.run(store.send('123',other['digest'],'test',httpx.MockTransport(failed)));self.assertEqual(len(calls),2)
 def test_snapshot_omits_roster_identities_and_declaration_answers(self):
  text=summary_text('123',None,[],[],dict(version=1,people=[dict(name='Private Learner',email='private@example.invalid',health='private health')]))
  self.assertIn('1 named delegates',text)
  for secret in ['Private Learner','private@example.invalid','private health']:self.assertNotIn(secret,text)
