"""Administrative selection only; complete source reads, no approvals or raw answer storage."""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from recruitment_checklist_access import FORMS
log=logging.getLogger('escs.automatic.followup')
log.setLevel(logging.INFO)

PAUSE='boolean_mm82qkbk'
ACTION='long_text_mm81znz1'
EXTRA=['name','status','acceptance','status7','status72','status247','status797','date_11',
       'dup__of_medical_disclosure','dup__of_added_to_maps__conveyance_','status64',
       'update_service_consent6','status58','color_mm2ffyt5','date13','date4',PAUSE,ACTION,'date_mm82q7gs','dbs_check_status','text_mm82r6we','date6']
GROUPS=['new_group','new_group88715','new_group72792','new_group35864','new_group29175',
        'new_group21426','new_group57757','new_group44955','topics']
CONTRACT_ROLES={
 'Healthcare Support Worker / Residential Support Worker - Fulltime':'contract_fulltime',
 'Mental Health Support Worker (Secure Transport) - Fulltime':'contract_fulltime',
 'Healthcare Support Worker / Residential Support Worker - Bank/Zero Hours':'contract_bank',
 'Mental Health Support Worker (Secure Transport) - Bank/Zero Hours':'contract_bank',
 'Control Room Operator':'contract_control',
}

def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC',value).casefold().split()) if isinstance(value,str) else ''

def names(value):
    if isinstance(value,dict):value=' '.join(str(value.get(k,'')) for k in ('first','middle','last'))
    value=normalized(value)
    if not value:return set()
    parts=value.split()
    return {value,parts[0]+' '+parts[-1]} if len(parts)>1 else {value}

class ReceiptIndex:
    def __init__(self,db):
        self.db=db
        db.executescript('''CREATE TABLE IF NOT EXISTS auto_source_state(
            kind TEXT PRIMARY KEY,watermark INTEGER NOT NULL,ready INTEGER NOT NULL,last_success TEXT);
            CREATE TABLE IF NOT EXISTS auto_source_keys(
            kind TEXT NOT NULL,submission_id TEXT NOT NULL,key_type TEXT NOT NULL,key_hash TEXT NOT NULL,
            PRIMARY KEY(kind,submission_id,key_type,key_hash));
            CREATE INDEX IF NOT EXISTS auto_source_lookup ON auto_source_keys(kind,key_type,key_hash);
            CREATE TABLE IF NOT EXISTS auto_index_config(key TEXT PRIMARY KEY,value TEXT NOT NULL);''')
        db.execute('INSERT OR IGNORE INTO auto_index_config VALUES(?,?)',('salt',secrets.token_hex(32)))
        self.salt=bytes.fromhex(db.execute('SELECT value FROM auto_index_config WHERE key=?',('salt',)).fetchone()[0]);db.commit()
        self.last_log={}

    def digest(self,value):return hmac.new(self.salt,value.encode(),hashlib.sha256).hexdigest()

    def keys(self,row):
        answers=row.get('answers')
        if not isinstance(answers,dict):raise ValueError('Source answers unavailable')
        result=set()
        for answer in answers.values():
            if not isinstance(answer,dict):raise ValueError('Source fields changed')
            title=normalized(answer.get('text','')).rstrip(':')
            value=answer.get('answer')
            if title in {'name','name of employee','applicant name'}:
                result.update(('name',self.digest(v)) for v in names(value))
            if title in {'email','e-mail','personal email address','email address','applicant email'}:
                value=normalized(value)
                if re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',value):result.add(('email',self.digest(value)))
        if not result:raise ValueError('Source receipt identity unavailable')
        return result

    async def refresh(self,source,now):
        limit=asyncio.Semaphore(3)
        async def one(kind,form):
            async with limit:
                state=self.db.execute('SELECT * FROM auto_source_state WHERE kind=?',(kind,)).fetchone()
                watermark=state['watermark'] if state and state['ready'] else 0
                largest=watermark;page_seen=set();rows=[]
                try:
                    for offset in range(0,50000,100):
                        page=await source.page(form,offset,100)
                        if not isinstance(page,list):raise ValueError('Source page unavailable')
                        ids=tuple(str(r.get('id')) for r in page if isinstance(r,dict))
                        if page and ids in page_seen:raise ValueError('Source pagination repeated')
                        page_seen.add(ids);older=False
                        for row in page:
                            if not isinstance(row,dict) or str(row.get('form_id'))!=form or not str(row.get('id','')).isdigit():
                                raise ValueError('Source identity changed')
                            sid=int(row['id']);largest=max(largest,sid)
                            if sid<=watermark:older=True;continue
                            if row.get('status')!='ACTIVE':continue
                            rows.extend((kind,str(sid),typ,key) for typ,key in self.keys(row))
                        if len(page)<100 or older:break
                    else:raise ValueError('Source scan incomplete')
                    with self.db:
                        self.db.executemany('INSERT OR IGNORE INTO auto_source_keys VALUES(?,?,?,?)',rows)
                        self.db.execute('INSERT OR REPLACE INTO auto_source_state VALUES(?,?,1,?)',
                                        (kind,largest,now.isoformat()))
                    if self.last_log.get(kind)!=('ready',largest):
                        log.info('Automatic receipt source ready: kind=%s',kind)
                        self.last_log[kind]=('ready',largest)
                    return True
                except Exception:
                    with self.db:self.db.execute('INSERT OR REPLACE INTO auto_source_state VALUES(?,?,0,NULL)',(kind,watermark))
                    if self.last_log.get(kind)!=('held',watermark):
                        log.info('Automatic receipt source held: kind=%s; absence not inferred',kind)
                        self.last_log[kind]=('held',watermark)
                    return False
        results=await asyncio.gather(*(one(k,f) for k,f in FORMS.items()))
        return sum(results)

    def receipt(self,kind,name,email,now):
        # Positive matches suppress a new request, never assert identity or approval.
        keys={('name',self.digest(v)) for v in names(name)}
        if normalized(email):keys.add(('email',self.digest(normalized(email))))
        for typ,key in keys:
            if self.db.execute('SELECT 1 FROM auto_source_keys WHERE kind=? AND key_type=? AND key_hash=? LIMIT 1',
                               (kind,typ,key)).fetchone():return 'found'
        row=self.db.execute('SELECT * FROM auto_source_state WHERE kind=?',(kind,)).fetchone()
        if not row or not row['ready'] or not row['last_success']:return 'unverified'
        if now-datetime.fromisoformat(row['last_success'])>timedelta(minutes=10):return 'unverified'
        return 'absent'

def values(item):return {c['id']:c for c in item.get('column_values',[])}
def text(cols,key):return cols.get(key,{}).get('text') or ''
def data(cols,key):return json.loads(cols.get(key,{}).get('value') or '{}')

def propose(item,index,now):
    from checklist_workflow import BOARD,REQUEST,RECEIVED,PRE_GROUPS,PRE_STATUS
    if str(item.get('board',{}).get('id'))!=BOARD or item.get('state')!='active':raise ValueError('Active pinned record required')
    cols=values(item);status=text(cols,'status53');group=item.get('group',{}).get('id')
    if data(cols,PAUSE).get('checked') in {True,'true'}:return set(),[],None
    if data(cols,REQUEST).get('ids') or data(cols,RECEIVED).get('ids'):return set(),[],None
    if group=='topics' and status in {'Employed with ESCS','Director'} or group=='new_group44955' and status=='Employee pending paperwork':
        stage='employed';due=now
    elif group in PRE_GROUPS and status in PRE_STATUS:
        if text(cols,'acceptance')!='Yes':return set(),['Awaiting applicant acceptance before automatic checks.'],None
        sent=data(cols,'offerletter_new_starter_jotform').get('date')
        if not sent:return set(),['Initial pre-employment checks must be sent before automatic chasing.'],None
        # Date-only sent fields: wait three complete days, avoiding an early chase.
        due=datetime.fromisoformat(sent).replace(hour=23,minute=59,tzinfo=ZoneInfo('Europe/London'))+timedelta(days=3)
        due=due.astimezone(timezone.utc);stage='pre_employment'
    elif group=='new_group' and status not in {'Leaver','Candidate Withdrawn','Rejected through process'}:
        return set(),['Awaiting recruitment acceptance and the initial check request.'],None
    else:return set(),[],None
    for col in ('date13','date4'):
        contacted=data(cols,col).get('date')
        if contacted:
            after=datetime.fromisoformat(contacted).replace(hour=23,minute=59,tzinfo=ZoneInfo('Europe/London'))+timedelta(days=3)
            due=max(due,after.astimezone(timezone.utc))
    email=data(cols,'email').get('email','')
    if (not isinstance(email,str) or normalized(email) in {'hr@securecareservices.co.uk','jobs@securecareservices.co.uk'}
            or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email) or not names(item.get('name',''))):
        return set(),['Verify the employee/applicant identity and email before requesting information.'],None
    missing={};review=[]
    rules=[('history','status7',{'No','None - Requested'}),
           ('medical','status72',{'Not Received','Requested'}),
           ('working_time','status247',{'Not Received','Requested'}),
           ('dress_code','dup__of_medical_disclosure',{'Not Received','Requested','Pending','Pending - Email Sent'}),
           ('driving_consent','status64',{'Requested'}),
           ('dbs_consent','update_service_consent6',{'Requested'})]
    for kind,col,labels in rules:
        label=text(cols,col)
        if label in labels or stage=='pre_employment' and not label and kind in {'history','medical','working_time','dress_code'}:
            missing[kind]=col
    if text(cols,'color_mm2ffyt5')=='Yes' and text(cols,'dup__of_added_to_maps__conveyance_') in {'Pending',''}:
        missing['driving_declaration']='dup__of_added_to_maps__conveyance_'
    if text(cols,'status797') in {'Sent','New Contract sent'}:
        kind=CONTRACT_ROLES.get(text(cols,'status'))
        if kind and data(cols,'date_11').get('date'):missing[kind]='status797'
        else:review.append('Confirm the issued role-specific contract before requesting a signature.')
    elif text(cols,'status797')=='Not Sent':review.append('HR must verify and issue the correct contract before signature chasing.')
    if text(cols,'status58') in {'Not Received','Sent','Awaiting'}:review.append('Verify encrypted payroll receipt with HR; do not request bank details by email.')
    if text(cols,'status7') in {'Partial - requested info','partial -reasons given','Received with some gaps'}:
        review.append('Review existing history and exceptions; a partial status alone must not trigger a new history request.')
    if text(cols,'status72')=='Out of Date':review.append('Check the existing medical renewal workflow and latest receipt before adding another campaign.')
    result=set()
    for kind in missing:
        receipt=index.receipt(kind,item.get('name',''),email,now)
        if receipt=='absent':result.add(kind)
        elif receipt=='found':review.append(kind+': existing submission found; reconcile its identity, completeness and applicable version before chasing.')
        else:review.append(kind+': source comparison is incomplete; HR review required before automatic selection.')
    return result,review,due

class AutomaticSelection:
    def __init__(self,worker):
        self.worker=worker;self.index=ReceiptIndex(worker.ledger.db)
        self.state={'enabled':True,'lastSuccess':None,'selected':0,'review':0}
        self.last_log=None
        from reconciliation_notices import ENABLED,Notices
        self.notices=Notices(self) if ENABLED else None
        from licence_expiry import ENABLED as LICENCE_ENABLED,LicenceExpiry
        self.licences=LicenceExpiry(self) if LICENCE_ENABLED else None
        from application_intake import ENABLED as INTAKE_ENABLED,ApplicationIntake
        self.intake=ApplicationIntake(self) if INTAKE_ENABLED else None

    async def roster(self):
        from checklist_workflow import COLUMNS
        fields='id name state created_at board { id } group { id } column_values(ids:'+json.dumps(sorted(set(COLUMNS+EXTRA)))+') { id type text value }'
        boards=(await self.worker.monday.call('query { boards(ids:[424237712]) { id items_page(limit:500,query_params:{rules:[{column_id:"group",operator:any_of,compare_value:'+json.dumps(GROUPS)+'}]}) { cursor items { '+fields+' } } } }'))['boards']
        if len(boards)!=1 or str(boards[0]['id'])!='424237712':raise ValueError('Roster board changed')
        page=boards[0]['items_page'];result={};cursors=set()
        for _ in range(30):
            for item in page['items']:
                if str(item.get('board',{}).get('id'))!='424237712' or item['id'] in result:raise ValueError('Roster identity changed')
                result[item['id']]=item
            cursor=page.get('cursor')
            if not cursor:return list(result.values())
            if cursor in cursors:raise ValueError('Roster pagination repeated')
            cursors.add(cursor)
            page=(await self.worker.monday.call('query($cursor:String!){next_items_page(limit:500,cursor:$cursor){cursor items {'+fields+'}}}',{'cursor':cursor}))['next_items_page']
        raise ValueError('Roster scan incomplete')

    async def fresh(self,item_id):
        from checklist_workflow import COLUMNS
        fields='id name state created_at board { id } group { id } column_values(ids:'+json.dumps(sorted(set(COLUMNS+EXTRA)))+') { id type text value }'
        rows=(await self.worker.monday.call('query($ids:[ID!]!){items(ids:$ids){'+fields+'}}',{'ids':[item_id]}))['items']
        if len(rows)!=1 or str(rows[0]['id'])!=item_id:raise ValueError('Fresh record unavailable')
        return rows[0]

    async def run(self,source,now):
        from checklist_workflow import REQUEST,LABELS,identity
        try:
            ready=await self.index.refresh(source,now)
            roster=await self.roster();counts={};name_counts={}
            for item in roster:
                key=normalized(data(values(item),'email').get('email',''));counts[key]=counts.get(key,0)+1
                for name in names(item.get('name','')):name_counts[name]=name_counts.get(name,0)+1
            from history_email_example import send_example
            await send_example(self,roster,now)
            if self.intake:await self.intake.run(source,roster,counts,name_counts,now)
            if self.licences:await self.licences.run(roster,counts,name_counts,now)
            if self.notices:await self.notices.run(roster,counts,name_counts,ready,now)
            selected=reviewed=0
            for item in roster:
                item_id=str(item['id'])
                if self.intake and self.intake.owned(item_id):continue
                if self.worker.ledger.db.execute('SELECT 1 FROM checklist_candidate WHERE id=?',(item_id,)).fetchone():continue
                kinds,review,due=propose(item,self.index,now)
                email=normalized(data(values(item),'email').get('email',''))
                if kinds and (counts[email]!=1 or any(name_counts[n]>1 for n in names(item.get('name','')))):
                    kinds=set();review.append('Duplicate identity/email requires HR review before automatic selection.')
                if review:reviewed+=1
                if not kinds and (not review or text(values(item),ACTION)):continue
                fresh=await self.fresh(item_id);again,_,due_again=propose(fresh,self.index,now)
                # Any relevant change cancels this proposal; no stale record mutation.
                if fresh.get('name')!=item.get('name') or values(fresh)!=values(item) or again!=kinds and kinds:continue
                writes={}
                if kinds:
                    ids=sorted(i for i,(kind,_) in LABELS.items() if kind in kinds)
                    writes[REQUEST]={'ids':ids}
                    merged=dict(fresh,column_values=[dict(c,value=json.dumps(writes[REQUEST])) if c['id']==REQUEST else c for c in fresh['column_values']])
                    try:identity(merged)
                    except (ValueError,TypeError,KeyError):continue
                if not text(values(fresh),ACTION):
                    note='Automatic receipt comparison: '+('Selected '+', '.join(sorted(kinds))+'. ' if kinds else '')+' '.join(review)
                    if due and due>now:note+=' First chase is held until '+due.isoformat()+'.'
                    writes[ACTION]={'text':note}
                if not writes:continue
                answer=await self.worker.monday.call('mutation($item:ID!,$value:JSON!){change_multiple_column_values(board_id:424237712,item_id:$item,column_values:$value){id}}',{'item':item_id,'value':json.dumps(writes)})
                if str(answer['change_multiple_column_values']['id'])!=item_id:raise ValueError('Automatic selection write unconfirmed')
                if kinds:
                    # Seed the existing ledger with the same guardrails and preserved delay.
                    self.worker.reconcile(merged,due_again if due_again and due_again>now else now,enroll=True)
                    selected+=1
            self.state.update(lastSuccess=now.isoformat(),sourcesReady=ready,sourceCount=len(FORMS),roster=len(roster),selected=selected,review=reviewed,error=None)
        except Exception:
            self.state.update(error='automatic_selection_requires_review')
        self.worker.state['automaticSelection']=dict(self.state)
        status=(self.state.get('sourcesReady'),self.state.get('roster'),self.state.get('selected'),self.state.get('review'),self.state.get('error'))
        if status!=self.last_log:
            log.info('Automatic follow-up selection: sources=%s roster=%s selected=%s review=%s error=%s',*status)
            self.last_log=status
        return dict(self.state)
