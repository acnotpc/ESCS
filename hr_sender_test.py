"""One agreed non-sensitive delivery test; no employee reminder activation."""
import logging
import os
import sqlite3
from email.utils import getaddresses
import httpx
from hr_mail_readiness import HRMailReadiness, HR
from recruitment_mail import RecruitmentMail

RECIPIENT = 'andy@securecareservices.co.uk'
TAG = '[ESCS-HR-TEST:20261012]'
SUBJECT = 'ESCS HR follow-up sender test ' + TAG
BODY = ('<p>This is a delivery test of ESCS employee follow-up emails sent from '
        'hr@securecareservices.co.uk.</p><p>No staff records have been changed and '
        'no personal information is requested.</p><p><a href="https://form.jotform.com/262844476768071">'
        'Open the isolated synthetic test form</a>. You do not need to submit it.</p>')
log = logging.getLogger('escs.hr.sender.test')
log.setLevel(logging.INFO)


class HRSenderTest:
    def __init__(self, path='/var/data/hr-mail-sender-test.sqlite', transport=None, readiness=None):
        self.path, self.transport = path, transport
        self.readiness = readiness or HRMailReadiness(transport)
        self.state = {'providerAccepted':False, 'deliveryFromHRVerified':False, 'staffRemindersEnabled':False}
        self.last_log = None

    async def call(self, method, params):
        if method not in {'mail.message.send','mail.mailbox.list','mail.message.list'}:
            raise ValueError('Unsupported fixed delivery test operation')
        connection = RecruitmentMail(os.getenv('BITRIX_RECRUITMENT_MAIL_REST_URL',''),1,self.transport)
        async with httpx.AsyncClient(timeout=30,follow_redirects=False,transport=self.transport) as client:
            r=await client.post(connection._url+'/'+method,json=params)
        p=r.json()
        if not r.is_success or not isinstance(p,dict) or p.get('error') or not isinstance(p.get('result'),dict):
            raise RuntimeError('Delivery test outcome requires review')
        return p['result']

    async def verify_delivery(self):
        boxes=(await self.call('mail.mailbox.list',{'email':RECIPIENT,'pagination':{'page':1,'limit':100}})).get('items',[])
        exact=[b for b in boxes if isinstance(b,dict) and str(b.get('email','')).lower()==RECIPIENT]
        if len(boxes)>=100 or len(exact)!=1 or type(exact[0].get('id')) is not int:
            return False
        mailbox_id=exact[0]['id']
        rows=(await self.call('mail.message.list',{'mailboxId':mailbox_id,'searchQuery':TAG,
                    'pagination':{'page':1,'limit':50}})).get('items',[])
        if not isinstance(rows,list) or len(rows)>=50:
            return False
        for row in rows:
            if not isinstance(row,dict) or row.get('mailboxId')!=mailbox_id or str(row.get('mailboxEmail','')).lower()!=RECIPIENT:
                continue
            senders={a.lower() for _,a in getaddresses([str(row.get('from',''))])}
            recipients={a.lower() for _,a in getaddresses([str(row.get('to',''))])}
            if row.get('subject')==SUBJECT and senders=={HR} and RECIPIENT in recipients:
                return True
        return False

    async def run(self):
        db=sqlite3.connect(self.path)
        db.execute('CREATE TABLE IF NOT EXISTS delivery_test(id TEXT PRIMARY KEY,state TEXT NOT NULL)')
        db.commit()
        try:
            existing=db.execute('SELECT state FROM delivery_test WHERE id=?',(TAG,)).fetchone()
            if not existing:
                ready=await self.readiness.check()
                if not all(ready.get(k) is True for k in ['mailboxVerified','senderVerified','nativeTestEmailsVerifiedOff']):
                    raise RuntimeError('HR sender or native settings require review')
                with db:
                    reserved=db.execute('INSERT OR IGNORE INTO delivery_test VALUES(?,?)',(TAG,'reserved')).rowcount
                if reserved:
                    try:
                        result=await self.call('mail.message.send',{'from':HR,'to':[RECIPIENT],
                                                                  'subject':SUBJECT,'body':BODY})
                        if result.get('success') is not True or result.get('to')!=[RECIPIENT]:
                            raise RuntimeError('Delivery test outcome unconfirmed')
                        with db:
                            db.execute('UPDATE delivery_test SET state=? WHERE id=?',('accepted',TAG))
                    except Exception:
                        with db:
                            db.execute('UPDATE delivery_test SET state=? WHERE id=?',('review',TAG))
                        raise RuntimeError('Delivery test outcome requires review; no automatic retry') from None
            stored=db.execute('SELECT state FROM delivery_test WHERE id=?',(TAG,)).fetchone()[0]
            self.state['providerAccepted']=stored=='accepted'
            if stored=='accepted':
                self.state['deliveryFromHRVerified']=await self.verify_delivery()
            self.state['requiresReview']=stored in {'reserved','review'}
            line='HR sender delivery test: accepted=%s receivedFromHR=%s requiresReview=%s staffReminders=false' % (
                str(self.state['providerAccepted']).lower(), str(self.state['deliveryFromHRVerified']).lower(),
                str(self.state['requiresReview']).lower())
        except Exception:
            self.state['requiresReview']=True
            line='HR sender delivery test requires review; no automatic retry staffReminders=false'
        finally:
            db.close()
        if line!=self.last_log:
            log.info(line)
            self.last_log=line
        return dict(self.state)
