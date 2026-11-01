import asyncio,json,os,tempfile,unittest
from unittest.mock import patch
import httpx
import test_mmva_trainer_picker as picker
from mmva_trainer_mail import TrainerResetMail
from test_mmva_monday_courses import CODE,reader

class RecoveryTests(unittest.TestCase):
 def test_public_request_same_response_and_no_password_or_token_leak(self):
  with tempfile.TemporaryDirectory() as folder:
   env,c=picker.PickerTests().setup_client(folder);sent=[]
   class Sender:
    async def send(self,invite):sent.append(invite)
   with patch.dict(os.environ,env),patch('mmva_attendance.TrainerResetMail',Sender):
    known=c.post('/attendance/trainer/forgot',json={'email':'trainer@example.invalid'})
    unknown=c.post('/attendance/trainer/forgot',json={'email':'missing@example.invalid'})
    self.assertEqual(known.status_code,200);self.assertEqual(known.json(),unknown.json());self.assertEqual(len(sent),1)
    self.assertNotIn('token',known.text)
    token=sent[0]['reset_path'].split('token=')[1]
    r=c.post('/attendance/trainer/reset',json={'token':token,'password':'a new test password here'})
    self.assertEqual(r.status_code,200);self.assertIn('Max-Age=0',r.headers['set-cookie'])
    self.assertEqual(c.post('/attendance/trainer/reset',json={'token':token,'password':'a new test password here'}).status_code,400)
    self.assertEqual(c.post('/attendance/trainer/login',json={'email':'trainer@example.invalid','password':'a new test password here'}).status_code,200)
 def test_fixed_mail_sender_and_encoded_reset_link(self):
  calls=[]
  def handler(req):
   method=req.url.path.split('/')[-1];data=json.loads(req.content);calls.append(method)
   if method=='mail.mailbox.list':result={'items':[{'id':103,'email':'info@mmva.co.uk'}]}
   elif method=='mail.mailbox.get':result={'item':{'id':103,'email':'info@mmva.co.uk'}}
   elif method=='mail.mailbox.senders':result={'items':[{'email':'info@mmva.co.uk'}]}
   elif method=='mail.message.send':
    self.assertEqual(data['from'],'info@mmva.co.uk');self.assertEqual(data['to'],['trainer@example.invalid'])
    self.assertIn('https://escs.onrender.com/attendance/trainer/reset#token=test-token',data['body'])
    self.assertIn('&lt;Trainer&gt;',data['body']);result={'success':True,'to':data['to']}
   else:self.fail(method)
   return httpx.Response(200,json={'result':result})
  with patch.dict(os.environ,{'BITRIX_RECRUITMENT_MAIL_REST_URL':'https://escs.bitrix24.com/rest/1/test'}):
   asyncio.run(TrainerResetMail(httpx.MockTransport(handler)).send({'email':'trainer@example.invalid','name':'<Trainer>','reset_path':'/attendance/trainer/reset#token=test-token'}))
  self.assertEqual(calls[-1],'mail.message.send')
 def test_excluded_course_not_resolved_or_listed(self):
  from fastapi import HTTPException
  with patch.dict(os.environ,{'MMVA_ATTENDANCE_EXCLUDED_COURSES':CODE}):
   with self.assertRaises(HTTPException):asyncio.run(reader().resolve(CODE,'2026-11-02'))
