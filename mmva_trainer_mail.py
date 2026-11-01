"""Fixed-purpose MMVA trainer recovery mail; no arbitrary recipient/content interface."""
import html
import os
import httpx
from recruitment_mail import RecruitmentMail

SENDER='info@mmva.co.uk'

class TrainerResetMail:
    def __init__(self,transport=None):
        self.transport=transport

    async def send(self,invite):
        connection=RecruitmentMail(os.getenv('BITRIX_RECRUITMENT_MAIL_REST_URL',''),103,self.transport)
        async with httpx.AsyncClient(timeout=20,follow_redirects=False,transport=self.transport) as client:
            async def call(method,params):
                response=await client.post(connection._url+'/'+method,json=params)
                payload=response.json()
                if not response.is_success or payload.get('error') or not isinstance(payload.get('result'),dict):
                    raise RuntimeError('MMVA recovery email unavailable')
                return payload['result']
            boxes=(await call('mail.mailbox.list',{'email':SENDER,'pagination':{'page':1,'limit':100}})).get('items',[])
            exact=[r for r in boxes if str(r.get('email','')).lower()==SENDER]
            if len(exact)!=1 or type(exact[0].get('id')) is not int:
                raise RuntimeError('MMVA sender requires review')
            mailbox=(await call('mail.mailbox.get',{'id':exact[0]['id'],'select':['id','email']})).get('item',{})
            if mailbox.get('id')!=exact[0]['id'] or str(mailbox.get('email','')).lower()!=SENDER:
                raise RuntimeError('MMVA sender requires review')
            found=False
            for page in range(1,11):
                rows=(await call('mail.mailbox.senders',{'pagination':{'page':page,'limit':100}})).get('items')
                if not isinstance(rows,list):raise RuntimeError('MMVA sender requires review')
                found=found or any(str(r.get('email','')).lower()==SENDER for r in rows)
                if len(rows)<100:break
            else:raise RuntimeError('MMVA sender requires review')
            if not found:raise RuntimeError('MMVA sender requires review')
            recipient=invite['email']
            # All values originate from the trainer registry, never request-supplied mail content.
            link='https://escs.onrender.com'+invite['reset_path']
            body='<p>Hello '+html.escape(invite['name'])+',</p><p>A password reset was requested for your MMVA trainer account.</p><p><a href="'+html.escape(link,quote=True)+'">Reset your MMVA trainer password</a></p><p>This private link expires in 30 minutes and works once. Choose a password of at least 15 characters. After changing it, sign in again at https://escs.onrender.com/attendance/trainer.</p><p>If you did not request this, ignore this email. Your current password remains unchanged until a reset is completed.</p><p>MMVA</p>'
            result=await call('mail.message.send',{'from':SENDER,'to':[recipient],'subject':'Reset your MMVA trainer password','body':body})
            if result.get('success') is not True or result.get('to')!=[recipient]:
                raise RuntimeError('MMVA recovery delivery unconfirmed')
