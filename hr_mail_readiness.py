"""Read-only HR sender and isolated test-form notification verification."""
import json
import logging
import os
import httpx
from recruitment_mail import RecruitmentMail

HR = 'hr@securecareservices.co.uk'
TEST_FORM = '262844476768071'
log = logging.getLogger('escs.hr.mail.readiness')
log.setLevel(logging.INFO)


class HRMailReadiness:
    def __init__(self, transport=None):
        self.transport = transport
        self.state = {'mailboxVerified': False, 'senderVerified': False,
                      'nativeTestEmailsVerifiedOff': False, 'sendingEnabled': False}
        self.last_log = None

    async def call(self, method, params):
        if method not in {'mail.mailbox.list', 'mail.mailbox.senders', 'mail.mailbox.get'}:
            raise ValueError('Unsupported HR metadata read')
        configured = RecruitmentMail(os.getenv('BITRIX_RECRUITMENT_MAIL_REST_URL', ''), 1, self.transport)
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, transport=self.transport) as client:
            response = await client.post(configured._url+'/'+method, json=params)
        payload = response.json()
        if not response.is_success or not isinstance(payload, dict) or payload.get('error') or not isinstance(payload.get('result'), dict):
            raise RuntimeError('HR metadata read unavailable')
        return payload['result']

    async def check_hr(self):
        rows = (await self.call('mail.mailbox.list', {'email':HR, 'pagination':{'page':1,'limit':100}})).get('items')
        if not isinstance(rows, list) or len(rows) >= 100:
            raise RuntimeError('Incomplete HR mailbox discovery')
        exact = [r for r in rows if isinstance(r, dict) and str(r.get('email', '')).lower() == HR]
        if len(exact) != 1 or type(exact[0].get('id')) is not int or exact[0]['id'] <= 0:
            raise RuntimeError('HR mailbox identity requires review')
        mailbox_id = exact[0]['id']
        row = (await self.call('mail.mailbox.get', {'id':mailbox_id,'select':['id','email']})).get('item')
        if not isinstance(row, dict) or row.get('id') != mailbox_id or str(row.get('email','')).lower() != HR:
            raise RuntimeError('HR mailbox identity changed')
        self.state.update(mailboxVerified=True, mailboxId=mailbox_id)
        found = False
        for page in range(1,11):
            senders = (await self.call('mail.mailbox.senders', {'pagination':{'page':page,'limit':100}})).get('items')
            if not isinstance(senders, list):
                raise RuntimeError('HR sender list unavailable')
            found |= any(isinstance(s, dict) and str(s.get('email','')).lower() == HR for s in senders)
            if len(senders) < 100:
                break
        else:
            raise RuntimeError('Incomplete sender list')
        self.state['senderVerified'] = found

    async def check_native(self):
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, transport=self.transport) as client:
            response = await client.get('https://eu-api.jotform.com/form/'+TEST_FORM+'/properties',
                headers={'APIKEY':os.getenv('JOTFORM_RECRUITMENT_READ_API_KEY','')})
        payload = response.json()
        props = payload.get('content') if isinstance(payload, dict) else None
        if not response.is_success or payload.get('responseCode') != 200 or not isinstance(props, dict):
            raise RuntimeError('Test notification settings unavailable')
        emails = props.get('emails')
        if isinstance(emails, str):
            try:
                emails = json.loads(emails)
            except ValueError:
                raise RuntimeError('Test notification settings unavailable') from None
        if isinstance(emails, dict):
            emails = list(emails.values())
        if not isinstance(emails, list) or any(not isinstance(e, dict) for e in emails):
            raise RuntimeError('Test notification settings unavailable')
        enabled = sum(str(e.get('disabled','')).lower() not in {'yes','1','true'}
                      and str(e.get('status','')).lower() != 'disabled' for e in emails)
        self.state.update(nativeTestEmailTemplates=len(emails),
                          nativeTestEnabledEmails=enabled, nativeTestEmailsVerifiedOff=enabled == 0)

    async def check(self):
        self.state.update(mailboxVerified=False, senderVerified=False, nativeTestEmailsVerifiedOff=False)
        try:
            await self.check_hr()
            self.state['mailError'] = None
        except Exception:
            self.state['mailError'] = 'hr_sender_requires_review'
        try:
            await self.check_native()
            self.state['nativeError'] = None
        except Exception:
            self.state['nativeError'] = 'native_settings_requires_review'
        line = 'HR follow-up readiness: mailbox=%s sender=%s testNativeEmailsOff=%s sending=false' % (
            str(self.state['mailboxVerified']).lower(), str(self.state['senderVerified']).lower(),
            str(self.state['nativeTestEmailsVerifiedOff']).lower())
        if line != self.last_log:
            log.info(line)
            self.last_log = line
        return dict(self.state)
