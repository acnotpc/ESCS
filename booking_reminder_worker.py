"""Bound-job timing reminders. No booking writes or dispatch decisions."""
import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from zoneinfo import ZoneInfo

import httpx
from conveyance_time_reminders import Evidence, Job, FIELDS, evaluate, aware
from control_room_message import send_control_room, verify_control_room
from team_sync import Binding

LONDON = ZoneInfo('Europe/London')
BOARD = '376064564'
STAGES = {s:s for s in ('arrived_d1','departed_d1','arrived_d2','departed_d2')}


class MondayReader:
    async def check(self):
        token = (os.getenv('MONDAY_CONVEYANCE_API_TOKEN') or os.getenv('MONDAY_API_TOKEN')
                 or os.getenv('MONDAY_RECRUITMENT_API_TOKEN'))
        if not token:
            raise RuntimeError('Monday connection unavailable')
        columns = sorted({c for names in FIELDS.values() for c in names} | {'text1'})
        query = 'query {boards(ids:[376064564]){id columns(ids:' + json.dumps(columns) + '){id type}}}'
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            r = await client.post('https://api.monday.com/v2', headers={
                'Authorization':token, 'API-Version':'2026-10'},json={'query':query})
        data=r.json()
        boards=data.get('data',{}).get('boards',[])
        if not r.is_success or data.get('errors') or len(boards)!=1 or str(boards[0]['id'])!=BOARD:
            raise RuntimeError('Monday booking connection unavailable')
        expected={c:('date' if c=='date1' else 'text' if c=='text1' else 'hour') for c in columns}
        if {c['id']:c['type'] for c in boards[0]['columns']} != expected:
            raise RuntimeError('Booking schema requires review')

    async def read(self, ids):
        token = (os.getenv('MONDAY_CONVEYANCE_API_TOKEN') or os.getenv('MONDAY_API_TOKEN')
                 or os.getenv('MONDAY_RECRUITMENT_API_TOKEN'))
        if not token:
            raise RuntimeError('Monday connection unavailable')
        columns = sorted({c for names in FIELDS.values() for c in names} | {'text1'})
        query = 'query($ids:[ID!]!){items(ids:$ids){id board{id} column_values(ids:' + json.dumps(columns) + '){id text}}}'
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            r = await client.post('https://api.monday.com/v2', headers={
                'Authorization':token, 'API-Version':'2026-10'},
                json={'query':query,'variables':{'ids':[str(i) for i in ids]}})
        data = r.json()
        if not r.is_success or data.get('errors') or not isinstance(data.get('data',{}).get('items'),list):
            raise RuntimeError('Monday read requires review')
        rows = data['data']['items']
        if len(rows) != len(ids) or {int(x['id']) for x in rows} != set(ids):
            raise RuntimeError('Incomplete Monday booking read')
        if any(str(x['board']['id']) != BOARD for x in rows):
            raise RuntimeError('Booking board mismatch')
        return {int(x['id']):{c['id']:c.get('text') for c in x['column_values']} for x in rows}


class ReminderWorker:
    def __init__(self, sync, monday=None, now=None):
        self.sync = sync
        self.monday = monday or MondayReader()
        self.now = now or (lambda:datetime.now(timezone.utc))
        self.lock = asyncio.Lock()
        self.last_run = None
        self.error = None
        self.last_result = None
        self.last_slot = None

    @property
    def enabled(self):
        return os.getenv('CONVEYANCE_REMINDERS_ENABLED') == 'true'

    @property
    def write(self):
        return os.getenv('CONVEYANCE_REMINDERS_WRITE_ENABLED') == 'true'

    def db(self):
        db = self.sync.ready().db
        db.executescript('''
          CREATE TABLE IF NOT EXISTS booking_reminder_runs (
            slot TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, body TEXT NOT NULL,
            state TEXT NOT NULL, message_id INTEGER, sent_at TEXT, acknowledged_at TEXT);
          CREATE TABLE IF NOT EXISTS booking_reminder_fields (
            day TEXT NOT NULL, fingerprint TEXT NOT NULL, slot TEXT NOT NULL,
            PRIMARY KEY(day,fingerprint));
        ''')
        return db

    async def source(self, current):
        db = self.db()
        bindings = [Binding.model_validate_json(r[0]) for r in db.execute('SELECT data FROM bindings')]
        selected = []
        for b in bindings:
            day = datetime.fromisoformat(b.service_date).date()
            if not 0 <= (current.astimezone(LONDON).date()-day).days <= 1:
                continue
            b.checked()
            if not b.first_meetings:
                continue
            rows = db.execute('SELECT * FROM milestones WHERE chat_id=?',(b.chat_id,)).fetchall()
            # Edited, deleted or ambiguous milestones stop reminders for this job.
            if any(r['status']=='review' for r in rows):
                continue
            events = [Evidence(STAGES[r['status']],r['occurred'],f'Bitrix chat{b.chat_id} message {r["message_id"]}')
                for r in rows if r['status'] in STAGES
                and timedelta(minutes=15) <= current-aware(r['occurred']) <= timedelta(hours=36)]
            if events:
                selected.append((b,events))
        ids = [b.monday_item_id for b,_ in selected]
        if len(ids) != len(set(ids)) or len(ids)>100:
            raise RuntimeError('Crew scope or booking volume requires review')
        if not ids:
            return []
        values = await self.monday.read(ids)
        jobs=[]
        for b,events in selected:
            cells=values[b.monday_item_id]
            if set(cells) != {c for names in FIELDS.values() for c in names} | {'text1'}:
                raise RuntimeError('Booking columns require review')
            jobno=re.sub(r'[^A-Z0-9]','',str(cells['text1']).upper())
            if jobno not in (b.job_number,b.job_number[3:]):
                raise RuntimeError('Booking identity mismatch')
            jobs.append(Job(b.monday_item_id,b.job_number,tuple(b.first_meetings),cells,events))
        return evaluate(jobs,current.isoformat())

    async def messages_since(self, oldest):
        rows=[]
        last=None
        for _ in range(20):
            params={'DIALOG_ID':'1037','LIMIT':50}
            if last:params['LAST_ID']=last
            data=await self.sync.call(self.sync.settings,'im.dialog.messages.get',params)
            page=data.get('messages') if isinstance(data,dict) else None
            if not isinstance(page,list):raise RuntimeError('Control Room history unavailable')
            if not page:return rows
            ids=[int(r['id']) for r in page]
            if last and min(ids)>=last:raise RuntimeError('History paging did not advance')
            rows.extend(r for r in page if int(r['id'])>oldest)
            if min(ids)<=oldest:return rows
            last=min(ids)
        raise RuntimeError('Control Room history incomplete')

    async def reconcile(self, db, current):
        pending=db.execute("SELECT * FROM booking_reminder_runs WHERE state='sent' AND acknowledged_at IS NULL").fetchall()
        if not pending:return
        rows=await self.messages_since(min(r['message_id'] for r in pending))
        for run in pending:
            for row in rows:
                if (int(row.get('author_id',0))==1037 and int(row['id'])>run['message_id']
                    and row.get('text','').strip()=='ACK '+run['token']):
                    stamp=aware(row['date'])
                    if aware(run['sent_at'])<=stamp<=current:
                        with db:db.execute('UPDATE booking_reminder_runs SET acknowledged_at=? WHERE slot=?',(stamp.isoformat(),run['slot']))
                        break

    async def run(self, preview=False):
        async with self.lock:
            current=self.now()
            if current.tzinfo is None:raise ValueError('Aware clock required')
            local=current.astimezone(LONDON)
            if not preview and (not self.enabled or not 6<=local.hour<=23):
                return {'state':'disabled_or_quiet_hours'}
            slot=local.strftime('%Y-%m-%dT%H')
            if not preview and self.last_slot == slot:
                return {'state':'hour_already_checked'}
            if not preview:self.last_slot=slot
            try:
                db=self.db()
                # Never proceed past an uncertain delivery without reconciliation.
                if db.execute("SELECT 1 FROM booking_reminder_runs WHERE state='sending' LIMIT 1").fetchone():
                    raise RuntimeError('Uncertain delivery requires reconciliation')
                await self.reconcile(db,current)
                await self.monday.check()
                async def preflight(method,params):return await self.sync.call(self.sync.settings,method,params)
                await verify_control_room(self.sync.settings,preflight)
                findings=await self.source(current)
                day=local.date().isoformat()
                slot=local.strftime('%Y-%m-%dT%H')
                token='ESCS-REM-'+local.strftime('%Y%m%d-%H')
                lines=[]; fingerprints=[]
                for finding in findings:
                    # Conflicting evidence must be resolved before chasing fields.
                    if finding['review']:continue
                    missing=[]
                    for m in finding['missing']:
                        fp=sha256(f'{finding["item_id"]}:{m["stage"]}'.encode()).hexdigest()
                        already=db.execute('''SELECT 1 FROM booking_reminder_fields f
                            JOIN booking_reminder_runs r ON r.slot=f.slot
                            WHERE f.fingerprint=? AND (f.day=? OR r.acknowledged_at IS NOT NULL)''',(fp,day)).fetchone()
                        if not already:
                            missing.append(m['field']); fingerprints.append(fp)
                    if missing:lines.append(f'{finding["job_number"]}: '+', '.join(missing)+'\n'+finding['url'])
                result={'state':'preview','jobs_checked':len(findings),'reminder_jobs':len(lines),'dispatch_ready':False}
                if not lines:result['state']='nothing_due'
                if lines and not preview and self.write:
                    body=('Andrew’s AI assistant — booking timing check\n\n'+'\n\n'.join(lines)+
                        '\n\nPlease update the missing fields from verified actual times. '
                        'Reply ACK '+token+' to acknowledge; acknowledgement does not mark fields complete. '
                        'Control Room retains all allocation and dispatch decisions.')
                    if len(body)>12000:raise RuntimeError('Reminder exceeds message limit')
                    with db:
                        added=db.execute('INSERT OR IGNORE INTO booking_reminder_runs(slot,token,body,state) VALUES (?,?,?,?)',(slot,token,body,'sending')).rowcount
                        if added:
                            db.executemany('INSERT INTO booking_reminder_fields VALUES (?,?,?)',[(day,fp,slot) for fp in fingerprints])
                    if added:
                        async def call(method,params):return await self.sync.call(self.sync.settings,method,params)
                        sent=await send_control_room(self.sync.settings,call,body)
                        rows=await self.messages_since(sent['messageId']-1)
                        match=[r for r in rows if int(r['id'])==sent['messageId'] and int(r.get('author_id',0))==1 and r.get('text')==body]
                        if len(match)!=1:raise RuntimeError('Sent message not verified')
                        stamp=aware(match[0]['date'])
                        if abs((stamp-current).total_seconds())>300:raise RuntimeError('Sent message timestamp requires review')
                        with db:db.execute("UPDATE booking_reminder_runs SET state='sent',message_id=?,sent_at=? WHERE slot=?",(sent['messageId'],stamp.isoformat(),slot))
                        result['state']='sent'
                    else:result['state']='already_processed'
                self.last_run=current.isoformat();self.last_result=result;self.error=None
                return result
            except Exception:
                self.last_run=current.isoformat()
                self.error='Booking reminders require source or delivery reconciliation'
                raise RuntimeError(self.error) from None

    def status(self):
        count = None
        try:
            count = self.db().execute('SELECT count(*) FROM bindings').fetchone()[0]
        except Exception:
            pass
        return {'enabled':self.enabled,'write_enabled':self.write,'last_run':self.last_run,
                'last_result':self.last_result,'error':self.error,'destination':'Control Room Team (1037)',
                'binding_count':count,
                'monday_connection_configured':bool(os.getenv('MONDAY_CONVEYANCE_API_TOKEN') or os.getenv('MONDAY_API_TOKEN') or os.getenv('MONDAY_RECRUITMENT_API_TOKEN')),
                'schedule':'Hourly 06:00–23:00 Europe/London','scope':'Verified bound-job D1/D2 milestones only'}

    async def loop(self):
        while True:
            try:await self.run()
            except Exception:pass
            await asyncio.sleep(60)
