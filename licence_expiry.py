"""Administrative expiry restriction and journalled employee renewal reminders."""
import html
import json
import logging
import os
from datetime import date
from zoneinfo import ZoneInfo

EXPIRY='date_mm82q7gs'
CHECK_STATUS='dbs_check_status'
REMINDER_STATUS='text_mm82r6we'
ENABLED=os.getenv('HR_LICENCE_EXPIRY_REMINDERS','').lower()=='true'
log=logging.getLogger('escs.licence.expiry');log.setLevel(logging.INFO)

def milestone(expiry,today):
    days=(expiry-today).days
    if days<=0 or days>60:return None
    return 7 if days<=7 else 30 if days<=30 else 60

def message(expiry,today):
    days=(expiry-today).days
    shown=expiry.strftime('%d %B %Y')
    return ('Our latest recorded DVLA result shows that your driving licence is valid to '+shown+
        ' ('+str(days)+' days remaining). Please arrange renewal before this date and contact HR so we can verify the renewed licence and carry out an updated online DVLA check.\n\n'
        'If the recorded valid-to date passes without verification of renewal, you will no longer be authorised to drive for ESCS until HR has verified your current licence and relevant driving entitlements.\n\n'
        'Where a valid driving licence is a requirement of your role, failure to renew could put your continued employment at risk. Any employment decision will be reviewed by HR and management; it is not automatic.\n\n'
        'If you have already renewed, or believe our recorded date is incorrect, please reply so HR can check the record and arrange a secure evidence check. Do not email identity documents or your driving licence number.')

class LicenceExpiry:
    def __init__(self,automatic):
        self.auto=automatic;self.worker=automatic.worker;self.db=self.worker.ledger.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS licence_expiry_attempt(
            item_id TEXT NOT NULL,expiry TEXT NOT NULL,milestone INTEGER NOT NULL,
            state TEXT NOT NULL,attempted_at TEXT NOT NULL,PRIMARY KEY(item_id,expiry,milestone))''')
        self.db.execute("UPDATE licence_expiry_attempt SET state='unknown' WHERE state='reserved'");self.db.commit();self.last=None

    async def write(self,item_id,updates):
        answer=await self.worker.monday.call('mutation($item:ID!,$value:JSON!){change_multiple_column_values(board_id:424237712,item_id:$item,column_values:$value){id}}',
            {'item':item_id,'value':json.dumps(updates)})
        if str(answer['change_multiple_column_values']['id'])!=item_id:raise ValueError('Licence expiry write unconfirmed')

    async def run(self,roster,counts,name_counts,now):
        from automatic_followup import values,data,text,names,normalized,PAUSE
        from checklist_workflow import identity
        today=now.astimezone(ZoneInfo('Europe/London')).date();result={'expired':0,'accepted':0,'held':0,'unknown':0}
        for item in roster:
            item_id=str(item['id']);cols=values(item)
            raw=data(cols,EXPIRY).get('date')
            if not raw:continue
            try:expiry=date.fromisoformat(raw)
            except (ValueError,TypeError):result['held']+=1;continue
            if item.get('state')!='active' or str(item.get('board',{}).get('id'))!='424237712':continue
            if expiry<today:
                result['expired']+=1
                if text(cols,CHECK_STATUS)=='Expired' and text(cols,REMINDER_STATUS).startswith('Expired - no driving for ESCS'):continue
                fresh=await self.auto.fresh(item_id)
                if fresh.get('name')!=item.get('name') or values(fresh)!=cols:continue
                await self.write(item_id,{CHECK_STATUS:{'label':'Expired'},REMINDER_STATUS:
                    'Expired - no driving for ESCS. Recorded valid to '+raw+'. HR must verify renewal/current DVLA result before restoring authorisation.'})
                continue
            group=item.get('group',{}).get('id');status=text(cols,'status53')
            if not (group=='topics' and status in {'Employed with ESCS','Director'} or group=='new_group44955' and status=='Employee pending paperwork'):continue
            point=milestone(expiry,today)
            if point is None:continue
            if text(cols,CHECK_STATUS)=='N/A - Non Driver':continue
            email=normalized(data(cols,'email').get('email',''))
            if (data(cols,PAUSE).get('checked') in {True,'true'} or counts.get(email)!=1
                or not names(item.get('name','')) or any(name_counts.get(n)!=1 for n in names(item.get('name','')))):
                result['held']+=1;continue
            try:
                _,stage,recipient,_,_,_=identity(item)
                if stage!='employed':continue
            except (ValueError,TypeError,KeyError):result['held']+=1;continue
            existing=self.db.execute('SELECT state FROM licence_expiry_attempt WHERE item_id=? AND expiry=? AND milestone=?',(item_id,raw,point)).fetchone()
            # Any unknown delivery for this expiry holds all further reminders for staff review.
            if self.db.execute("SELECT 1 FROM licence_expiry_attempt WHERE item_id=? AND expiry=? AND state IN ('unknown','reserved')",(item_id,raw)).fetchone():
                result['unknown']+=1;continue
            if existing:continue
            fresh=await self.auto.fresh(item_id)
            if fresh.get('name')!=item.get('name') or values(fresh)!=cols:continue
            with self.db:self.db.execute("INSERT INTO licence_expiry_attempt VALUES(?,?,?,'reserved',?)",(item_id,raw,point,now.isoformat()))
            try:
                body='<p>'+html.escape(message(expiry,today)).replace('\n\n','</p><p>')+'</p>'
                await self.worker.sender.send('employed',recipient,'Driving licence renewal reminder - valid to '+expiry.strftime('%d %B %Y'),body)
            except Exception:
                with self.db:self.db.execute("UPDATE licence_expiry_attempt SET state='unknown' WHERE item_id=? AND expiry=? AND milestone=?",(item_id,raw,point))
                result['unknown']+=1
                await self.write(item_id,{REMINDER_STATUS:'Delivery uncertain - '+str(point)+'-day reminder for '+raw+'. Do not automatically resend; HR review required.'})
            else:
                with self.db:self.db.execute("UPDATE licence_expiry_attempt SET state='accepted' WHERE item_id=? AND expiry=? AND milestone=?",(item_id,raw,point))
                result['accepted']+=1
                await self.write(item_id,{REMINDER_STATUS:'Provider accepted '+str(point)+'-day renewal reminder; valid to '+raw+'; '+now.isoformat()})
        self.worker.state['licenceExpiry']=dict(result,lastSuccess=now.isoformat())
        if result!=self.last:log.info('Driving licence expiry check: expired=%s accepted=%s held=%s uncertain=%s',result['expired'],result['accepted'],result['held'],result['unknown']);self.last=result
        return result
