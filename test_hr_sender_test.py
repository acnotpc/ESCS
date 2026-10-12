import tempfile
import unittest
from unittest.mock import patch
import httpx
from hr_sender_test import HRSenderTest, HR, RECIPIENT, SUBJECT


class Ready:
    async def check(self):
        return dict.fromkeys(['mailboxVerified','senderVerified','nativeTestEmailsVerifiedOff'],True)


class SenderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=self.tmp.name+'/sender.sqlite'
        self.sends=0
        self.fail=False
        self.from_hr=True
        self.env=patch.dict('os.environ',{'BITRIX_RECRUITMENT_MAIL_REST_URL':'https://escs.bitrix24.com/rest/1/test-only/'})
        self.env.start()

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()

    def handler(self,request):
        import json
        params=json.loads(request.content)
        if request.url.path.endswith('mail.message.send'):
            self.sends+=1
            self.assertEqual(HR,params['from'])
            self.assertEqual([RECIPIENT],params['to'])
            self.assertNotIn('bcc',params)
            if self.fail: return httpx.Response(500,json={'error':'secret provider failure'})
            result={'success':True,'to':[RECIPIENT]}
        elif request.url.path.endswith('mail.mailbox.list'):
            result={'items':[{'id':8,'email':RECIPIENT}]}
        else:
            result={'items':[{'mailboxId':8,'mailboxEmail':RECIPIENT,'subject':SUBJECT,
                'from':HR if self.from_hr else 'noreply@jotform.com','to':RECIPIENT}]}
        return httpx.Response(200,json={'result':result})

    def worker(self):
        return HRSenderTest(self.path,httpx.MockTransport(self.handler),Ready())

    async def test_exact_sender_received_and_no_replay(self):
        result=await self.worker().run()
        self.assertTrue(result['providerAccepted'])
        self.assertTrue(result['deliveryFromHRVerified'])
        await self.worker().run()
        self.assertEqual(1,self.sends)

    async def test_unknown_delivery_never_resends(self):
        self.fail=True
        result=await self.worker().run()
        self.assertTrue(result['requiresReview'])
        await self.worker().run()
        self.assertEqual(1,self.sends)

    async def test_noreply_is_not_verified_as_hr(self):
        self.from_hr=False
        result=await self.worker().run()
        self.assertTrue(result['providerAccepted'])
        self.assertFalse(result['deliveryFromHRVerified'])

    async def test_readiness_blocks_before_send(self):
        class NotReady:
            async def check(self): return {'senderVerified':False}
        result=await HRSenderTest(self.path,httpx.MockTransport(self.handler),NotReady()).run()
        self.assertTrue(result['requiresReview'])
        self.assertEqual(0,self.sends)
