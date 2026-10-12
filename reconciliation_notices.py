"""One-off, journalled receipt reconciliation; no document requests or approvals."""
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta

STATUS='text_mm82ewas'
ENABLED=os.getenv('HR_FOLLOWUP_RECONCILIATION_NOTICES','').lower()=='true'
WORDING=('If you believe this information has already been submitted, please reply to this email '
         'so we can check our spam/junk folders and any recording errors. '
         'Please tell us which form and roughly when you submitted it.')
BODY=('We are checking the information recorded against your staff/recruitment record. '
      'This does not mean you need to submit documents again.\n\n'+WORDING+
      '\n\nPlease do not email bank details, medical information or identity documents. '
      'Our team will check your record and contact you if anything further is needed.')
log=logging.getLogger('escs.reconciliation');log.setLevel(logging.INFO)

def contact(item,counts,name_counts):
    from automatic_followup import values,text,data,names,normalized,PAUSE
    from checklist_workflow import BOARD,REQUEST,RECEIVED,PRE_GROUPS,PRE_STATUS
    cols=values(item);group=item.get('group',{}).get('id');status=text(cols,'status53')
    if str(item.get('board',{}).get('id'))!=BOARD or item.get('state')!='active':return None
    if data(cols,PAUSE).get('checked') in {True,'true'}:return None
    if data(cols,REQUEST).get('ids') or data(cols,RECEIVED).get('ids'):return None
    if text(cols,'acceptance') in {'No','Sent Closed Email','Sent closing email'}:return None
    if group=='topics' and status in {'Employed with ESCS','Director'} or group=='new_group44955' and status=='Employee pending paperwork':stage='employed'
    elif group in PRE_GROUPS and status in PRE_STATUS or group=='new_group' and status=='Pending Review':stage='pre_employment'
    else:return None
    email=data(cols,'email').get('email','');key=normalized(email)
    if (cols.get('email',{}).get('type')!='email' or not isinstance(email,str) or not email.isascii()
        or not re.fullmatch(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+',email)
        or key in {'hr@securecareservices.co.uk','jobs@securecareservices.co.uk'}
        or counts.get(key)!=1 or not names(item.get('name',''))
        or any(name_counts.get(n)!=1 for n in names(item.get('name','')))):return None
    return stage,email

class Notices:
    def __init__(self,automatic):
        self.auto=automatic;self.worker=automatic.worker;self.db=self.worker.ledger.db
        self.db.executescript('''CREATE TABLE IF NOT EXISTS reconciliation_scope(item_id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS reconciliation_notice(item_id TEXT PRIMARY KEY,state TEXT NOT NULL,
        attempted_at TEXT,accepted_at TEXT,stage TEXT,recipient_digest TEXT);
        CREATE TABLE IF NOT EXISTS reconciliation_config(key TEXT PRIMARY KEY,value TEXT);''')
        self.db.execute("UPDATE reconciliation_notice SET state='unknown' WHERE state='reserved'");self.db.commit()
        self.last=None

    async def status(self,item_id,state,now):
        labels={'queued':'Queued for one-off record check','held':'Held: contact/stage or existing campaign needs review',
                'accepted':'Provider accepted one-off record check','unknown':'Delivery uncertain: do not automatically resend'}
        answer=await self.worker.monday.call('mutation($item:ID!,$value:JSON!){change_multiple_column_values(board_id:424237712,item_id:$item,column_values:$value){id}}',
            {'item':item_id,'value':json.dumps({STATUS:labels[state]+'; '+now.isoformat()})})
        if str(answer['change_multiple_column_values']['id'])!=item_id:raise ValueError('Notice status unconfirmed')

    async def run(self,roster,counts,name_counts,ready,now):
        from automatic_followup import propose,values
        from recruitment_checklist_access import FORMS
        if ready!=len(FORMS):return
        if not self.db.execute("SELECT 1 FROM reconciliation_config WHERE key='frozen'").fetchone():
            cohort=[]
            for item in roster:
                item_id=str(item['id'])
                if self.db.execute('SELECT 1 FROM checklist_candidate WHERE id=?',(item_id,)).fetchone():continue
                kinds,review,_=propose(item,self.auto.index,now)
                if review and not kinds:cohort.append((item_id,))
            with self.db:
                self.db.executemany('INSERT OR IGNORE INTO reconciliation_scope VALUES(?)',cohort)
                self.db.executemany("INSERT OR IGNORE INTO reconciliation_notice(item_id,state) VALUES(?,'queued')",cohort)
                self.db.execute("INSERT INTO reconciliation_config VALUES('frozen',?)",(now.isoformat(),))
        by_id={str(i['id']):i for i in roster};attempts=0
        for row in self.db.execute("SELECT * FROM reconciliation_notice WHERE state='queued' ORDER BY item_id").fetchall():
            item_id=row['item_id'];item=by_id.get(item_id)
            target=contact(item,counts,name_counts) if item else None
            if self.db.execute('SELECT 1 FROM checklist_candidate WHERE id=?',(item_id,)).fetchone():target=None
            if not target:
                with self.db:self.db.execute("UPDATE reconciliation_notice SET state='held' WHERE item_id=?",(item_id,))
                await self.status(item_id,'held',now);continue
            if attempts>=25:continue
            fresh=await self.auto.fresh(item_id)
            if fresh.get('name')!=item.get('name') or values(fresh)!=values(item) or contact(fresh,counts,name_counts)!=target:continue
            stage,email=target
            # Commit before external effect. Any exception/restart is delivery-uncertain, never retried.
            with self.db:self.db.execute("UPDATE reconciliation_notice SET state='reserved',attempted_at=?,stage=?,recipient_digest=? WHERE item_id=? AND state='queued'",
                (now.isoformat(),stage,self.auto.index.digest(email.casefold()),item_id))
            attempts+=1
            try:
                await self.worker.sender.send(stage,email,'Please help us check your staff/recruitment record',
                    '<p>'+html.escape(BODY).replace('\n\n','</p><p>')+'</p>')
            except Exception:
                with self.db:self.db.execute("UPDATE reconciliation_notice SET state='unknown' WHERE item_id=?",(item_id,))
                await self.status(item_id,'unknown',now)
            else:
                with self.db:self.db.execute("UPDATE reconciliation_notice SET state='accepted',accepted_at=? WHERE item_id=?",(now.isoformat(),item_id))
                await self.status(item_id,'accepted',now)
        result=dict(self.db.execute('SELECT state,count(*) FROM reconciliation_notice GROUP BY state').fetchall())
        self.worker.state['reconciliationNotices']=result
        if result!=self.last:log.info('One-off reconciliation notices: counts=%s',result);self.last=result

def apply_budget(worker,item_id,now):
    db=worker.ledger.db
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reconciliation_notice'").fetchone():return
    row=db.execute('SELECT * FROM reconciliation_notice WHERE item_id=?',(item_id,)).fetchone()
    if not row:return
    if row['state']=='accepted':
        candidate=db.execute('SELECT next_due FROM checklist_candidate WHERE id=?',(item_id,)).fetchone()
        due=max(datetime.fromisoformat(candidate['next_due']),datetime.fromisoformat(row['accepted_at'])+timedelta(hours=72))
        with db:
            db.execute('UPDATE checklist_candidate SET sends=max(sends,1),next_due=? WHERE id=?',(due.isoformat(),item_id))
            db.execute("INSERT INTO checklist_attempt(candidate_id,attempted_at,status) VALUES(?,?,'accepted')",(item_id,row['attempted_at']))
    elif row['state'] in {'reserved','unknown'}:
        with db:db.execute("INSERT INTO checklist_attempt(candidate_id,attempted_at,status) VALUES(?,?,'unknown')",(item_id,row['attempted_at']))
        worker.hold(item_id,'One-off record-check delivery is uncertain; do not resend automatically.')
