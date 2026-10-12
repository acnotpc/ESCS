import json
import unittest
from unittest.mock import patch
import httpx
from hr_mail_readiness import HRMailReadiness, HR


class HRReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def run_probe(self, wrong=False, native=None):
        def handle(request):
            if request.url.host == 'eu-api.jotform.com':
                return httpx.Response(200,json={'responseCode':200,'content':{'emails':native or []}})
            self.assertIn('/rest/api/1/',request.url.path)
            self.assertNotIn('mail.message',request.url.path)
            if request.url.path.endswith('mail.mailbox.list'):
                data={'items':[{'id':5,'email':HR}]}
            elif request.url.path.endswith('mail.mailbox.get'):
                data={'item':{'id':5,'email':'jobs@securecareservices.co.uk' if wrong else HR}}
            else:
                data={'items':[{'email':HR}]}
            return httpx.Response(200,json={'result':data})
        with patch.dict('os.environ',{'BITRIX_RECRUITMENT_MAIL_REST_URL':'https://escs.bitrix24.com/rest/1/test-only/'}):
            return await HRMailReadiness(httpx.MockTransport(handle)).check()

    async def test_hr_identity_and_sender_read_only(self):
        result=await self.run_probe()
        self.assertTrue(result['mailboxVerified'])
        self.assertTrue(result['senderVerified'])
        self.assertTrue(result['nativeTestEmailsVerifiedOff'])
        self.assertFalse(result['sendingEnabled'])

    async def test_jobs_mailbox_cannot_be_used_as_hr(self):
        result=await self.run_probe(wrong=True)
        self.assertFalse(result['mailboxVerified'])
        self.assertFalse(result['senderVerified'])

    async def test_default_native_notification_is_not_off(self):
        result=await self.run_probe(native=[{'type':'notification','from':'default'}])
        self.assertFalse(result['nativeTestEmailsVerifiedOff'])

    async def test_explicitly_disabled_native_email_is_off(self):
        result=await self.run_probe(native=[{'type':'notification','disabled':'Yes'}])
        self.assertTrue(result['nativeTestEmailsVerifiedOff'])

    async def test_unsupported_send_is_rejected(self):
        with self.assertRaises(ValueError):
            await HRMailReadiness().call('mail.message.send',{})
