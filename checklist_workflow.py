"""Board-pinned administrative follow-up. Explicit selections only; no hiring decisions."""
import asyncio
import html
import json
import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlencode
import httpx
from fastapi import APIRouter, Depends
from recruitment_checklist import Checklist, REQUIREMENTS, message
from recruitment_checklist_access import ChecklistRead, FORMS, SIGNATURES, CHOICE
from recruitment_tracking import TrackingLinks, TRACKING
from recruitment_receipts import ReceiptHold, field, EXTENDED_ENABLED, EXTENDED_REQUESTS
from hr_mail_readiness import HRMailReadiness, HR
from recruitment_mail import RecruitmentMail, JOBS_EMAIL
from history_completeness import analyse as analyse_history, check_schema as check_history_schema
from zoneinfo import ZoneInfo

BOARD = '424237712'
REQUEST = 'dropdown_mm82vwzc'
RECEIVED = 'dropdown_mm82k2cm'
SUMMARY = 'long_text_mm82ms8e'
RESUME = 'boolean_mm8241bm'
LABELS = {1: ('history', '10-year history'), 2: ('medical', 'Medical disclosure'),
          3: ('right_to_work', 'Right to work'), 4: ('working_time', '48-hour choice'),
          5: ('dbs', 'DBS application details')}
if EXTENDED_ENABLED:
    LABELS.update({i:(row[0],row[1]) for i,row in EXTENDED_REQUESTS.items()})
PRE_GROUPS = {'new_group88715', 'new_group72792', 'new_group35864', 'new_group29175',
              'new_group21426', 'new_group57757'}
PRE_STATUS = {'To be processed by Recruitment', 'Awaiting DBS', 'DBS awaiting ZC approval',
              'Awaiting 3rd ref for DBS', 'Awaiting Training', 'Awaiting Taster Shift',
              'Awaiting 2nd Training', 'Awaiting Start Date'}
COLUMNS = [REQUEST, RECEIVED, SUMMARY, RESUME, 'email', 'status53', 'offerletter_new_starter_jotform']
AUTO_SELECT=os.getenv('HR_FOLLOWUP_AUTO_SELECT','').lower()=='true'
AUTO_PAUSE='boolean_mm82qkbk'
if AUTO_SELECT:COLUMNS.append(AUTO_PAUSE)
ITEM_FIELDS = 'id state board { id } group { id } column_values(ids:' + json.dumps(COLUMNS) + ') { id type text value }'
log = logging.getLogger('escs.checklist.workflow')
log.setLevel(logging.INFO)


def selections(column):
    if column.get('type') != 'dropdown':
        raise ValueError('Follow-up selection schema changed')
    raw = json.loads(column.get('value') or '{}')
    ids = raw.get('ids', [])
    if not isinstance(ids, list) or any(type(i) is not int or i not in LABELS for i in ids):
        raise ValueError('Unknown follow-up selection')
    return {LABELS[i][0] for i in ids}


def identity(item):
    if (str(item.get('board', {}).get('id')) != BOARD or item.get('state') != 'active'
            or not str(item.get('id', '')).isdigit()):
        raise ValueError('Active recruitment record required')
    columns = {c['id']: c for c in item['column_values']}
    if len(columns) != len(item['column_values']):
        raise ValueError('Ambiguous columns')
    if AUTO_SELECT and str(json.loads(columns.get(AUTO_PAUSE,{}).get('value') or '{}').get('checked','')).lower()=='true':
        raise ValueError('Automatic follow-up paused for this person.')
    status = columns['status53']['text']
    group = item.get('group', {}).get('id')
    if (group == 'topics' and status in {'Employed with ESCS', 'Director'}
            or group == 'new_group44955' and status == 'Employee pending paperwork'):
        stage = 'employed'
    elif group in PRE_GROUPS and status in PRE_STATUS:
        stage = 'pre_employment'
    else:
        raise ValueError('Employment stage requires staff review')
    if columns['email'].get('type') != 'email':
        raise ValueError('Email column schema changed')
    email = json.loads(columns['email'].get('value') or '{}').get('email', '').strip().lower()
    if (not re.fullmatch(r'[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', email)
            or email in {HR, JOBS_EMAIL} or len(email) > 254):
        raise ValueError('Verified single staff email required')
    requested, received = selections(columns[REQUEST]), selections(columns[RECEIVED])
    if len([kind for kind in requested if kind.startswith('contract_')]) > 1:
        raise ValueError('Select the single approved contract for this employee.')
    if stage == 'pre_employment' and 'medical' in requested:
        sent = json.loads(columns['offerletter_new_starter_jotform'].get('value') or '{}')
        if not sent.get('date'):
            raise ValueError('Medical request stage requires staff review')
    return str(item['id']), stage, email, requested, received, columns[SUMMARY].get('text')


class MondayFollowup:
    def __init__(self, transport=None):
        self.transport = transport

    async def call(self, query, variables=None):
        token = os.getenv('MONDAY_RECRUITMENT_API_TOKEN', '')
        if not token:
            raise RuntimeError('Monday connection unavailable')
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, transport=self.transport) as client:
            response = await client.post('https://api.monday.com/v2', headers={
                'Authorization': token, 'API-Version': '2026-10'}, json={'query':query, 'variables':variables or {}})
        payload = response.json()
        if not response.is_success or not isinstance(payload, dict) or payload.get('errors') or not isinstance(payload.get('data'), dict):
            raise RuntimeError('Monday follow-up operation failed')
        return payload['data']

    async def schema(self):
        boards = (await self.call('query { boards(ids:[424237712]) { id columns(ids:' +
            json.dumps(COLUMNS) + ') { id type settings } } }'))['boards']
        if len(boards) != 1 or str(boards[0]['id']) != BOARD:
            raise RuntimeError('Board scope changed')
        cols = {c['id']:c for c in boards[0]['columns']}
        expected = {REQUEST:'dropdown', RECEIVED:'dropdown', SUMMARY:'long_text', RESUME:'checkbox', 'email':'email',
                    'status53':'status', 'offerletter_new_starter_jotform':'date'}
        if AUTO_SELECT:expected[AUTO_PAUSE]='checkbox'
        if {k:v['type'] for k,v in cols.items()} != expected:
            raise RuntimeError('Follow-up column types changed')
        for key in (REQUEST, RECEIVED):
            labels = cols[key]['settings']['labels']
            actual = {int(x['id']):x.get('name', x.get('label')) for x in labels}
            expected = {i:l for i,(_,l) in LABELS.items()}
            # During rollout, additional unselected labels must not break base campaigns.
            allowed_extra = {i:row[1] for i,row in EXTENDED_REQUESTS.items()}
            if any(actual.get(i)!=label for i,label in expected.items()) or any(
                    i not in expected and allowed_extra.get(i)!=label for i,label in actual.items()):
                raise RuntimeError('Follow-up labels changed')
        return True

    async def requested(self):
        query = ('query { boards(ids:[424237712]) { id items_page(limit:100,query_params:{rules:[{column_id:"' + REQUEST +
            '",operator:is_not_empty,compare_value:[]}]}) { cursor items { ' + ITEM_FIELDS + ' } } } }')
        boards = (await self.call(query))['boards']
        if len(boards) != 1 or str(boards[0]['id']) != BOARD:
            raise RuntimeError('Board scope changed')
        page = boards[0]['items_page']
        rows, cursors = {}, set()
        for _ in range(100):
            for item in page['items']:
                if str(item.get('board',{}).get('id')) != BOARD or str(item['id']) in rows:
                    raise RuntimeError('Request page identity changed')
                rows[str(item['id'])] = item
            cursor = page.get('cursor')
            if not cursor:
                return list(rows.values())
            if cursor in cursors:
                raise RuntimeError('Incomplete request pagination')
            cursors.add(cursor)
            page = (await self.call('query($cursor:String!){next_items_page(limit:100,cursor:$cursor){cursor items {' +
                ITEM_FIELDS + '}}}', {'cursor':cursor}))['next_items_page']
        raise RuntimeError('Request scan incomplete')

    async def item(self, item_id):
        if not str(item_id).isdigit():
            raise ValueError('Numeric record required')
        rows = (await self.call('query($ids:[ID!]!){items(ids:$ids){' + ITEM_FIELDS + '}}', {'ids':[item_id]}))['items']
        if len(rows) != 1 or str(rows[0]['id']) != item_id or str(rows[0].get('board',{}).get('id')) != BOARD:
            raise RuntimeError('Staff record unavailable')
        return rows[0]

    async def summary(self, item_id, text):
        if not item_id.isdigit():
            raise ValueError('Numeric record required')
        # Only the dedicated administrative progress column is writable.
        data = await self.call('mutation($item:ID!,$value:JSON!){change_multiple_column_values('
            'board_id:424237712,item_id:$item,column_values:$value){id}}',
            {'item':item_id, 'value':json.dumps({SUMMARY:{'text':text}})})
        if str(data['change_multiple_column_values']['id']) != item_id:
            raise RuntimeError('Progress write unconfirmed')

    async def clear_resume(self, item_id):
        if not item_id.isdigit():
            raise ValueError('Numeric record required')
        data = await self.call('mutation($item:ID!,$value:JSON!){change_multiple_column_values('
            'board_id:424237712,item_id:$item,column_values:$value){id}}',
            {'item':item_id,'value':json.dumps({RESUME:None})})
        if str(data['change_multiple_column_values']['id']) != item_id:
            raise RuntimeError('Resume acknowledgement unconfirmed')


class WorkflowSource(ChecklistRead):
    async def page(self, form_id, offset=0, limit=100):
        if form_id not in FORMS.values() or type(offset) is not int or offset < 0 or limit not in {1,100}:
            raise ValueError('Pinned receipt scan required')
        rows = await self._get('/form/' + form_id + '/submissions?' + urlencode({
            'orderby':'id', 'offset':offset, 'limit':limit}))
        if not isinstance(rows, list) or len(rows) > limit:
            raise RuntimeError('Invalid receipt page')
        return rows

    async def after(self, form_id, baseline):
        found, previous = {}, None
        for offset in range(0,10000,100):
            rows = await self.page(form_id, offset)
            reached = False
            for row in rows:
                if not isinstance(row,dict) or str(row.get('form_id')) != form_id or not str(row.get('id','')).isdigit():
                    raise RuntimeError('Receipt page identity changed')
                sid = int(row['id'])
                if previous is not None and sid > previous:
                    raise RuntimeError('Receipt page ordering changed')
                previous = sid
                if sid <= baseline:
                    reached = True
                else:
                    found[sid] = row
            if reached or len(rows) < 100:
                return list(found.values())
        raise RuntimeError('Receipt scan incomplete')


class FollowupSender:
    def __init__(self, transport=None):
        self.transport = transport

    async def send(self, stage, recipient, subject, body):
        if stage not in {'pre_employment','employed'}:
            raise ValueError('Known stage required')
        connection = RecruitmentMail(os.getenv('BITRIX_RECRUITMENT_MAIL_REST_URL',''),
                                     int(os.getenv('BITRIX_RECRUITMENT_MAILBOX_ID','107')), self.transport)
        if stage == 'employed':
            readiness = HRMailReadiness(self.transport)
            await readiness.check_hr()
            if not readiness.state['mailboxVerified'] or not readiness.state['senderVerified']:
                raise RuntimeError('HR sender unavailable')
            sender = HR
        else:
            await connection.verify_mailbox()
            sender = JOBS_EMAIL
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, transport=self.transport) as client:
            response = await client.post(connection._url + '/mail.message.send', json={
                'from':sender, 'to':[recipient], 'subject':subject, 'body':body})
        payload = response.json()
        result = payload.get('result') if isinstance(payload,dict) else None
        if (not response.is_success or not isinstance(result,dict) or result.get('success') is not True
                or result.get('to') != [recipient]):
            raise RuntimeError('Follow-up delivery requires staff review')


def render(kinds, stage, links, history_issues=None):
    text = message(kinds, stage)
    if stage == 'employed':
        text = text.replace('contact Recruitment to arrange', 'contact HR to arrange')
    # DBS detail/document receipt is recorded by staff; the generic new-starter
    # form is deliberately not treated as proof all required details are present.
    if 'dbs' in kinds:
        text = text.replace(REQUIREMENTS['dbs'][1], 'Please contact ' + ('HR' if stage == 'employed' else 'Recruitment') +
            ' to confirm the outstanding details and arrange the secure document-check route.')
    body = html.escape(text)
    for kind, url in links.items():
        body = body.replace(html.escape(REQUIREMENTS[kind][1]), '<a href="' + html.escape(url, quote=True) + '">Open secure form</a>')
    for kind in set(kinds)-set(links):
        url=REQUIREMENTS[kind][1]
        if url and url in text:
            body=body.replace(html.escape(url), '<a href="'+html.escape(url,quote=True)+'">Open secure form</a>')
    rendered='<p>' + body.replace('\n\n','</p><p>') + '</p>'
    if history_issues and 'history' in kinds:
        rendered += '<p>Your history was received. Please supply these missing details in a corrected full history, keeping the periods already supplied:</p><ul>' + ''.join('<li>'+html.escape(issue)+'</li>' for issue in history_issues) + '</ul>'
    return rendered


class ChecklistWorkflow:
    def __init__(self, path='/var/data/recruitment-checklist.sqlite', monday=None, source=None, sender=None):
        self.ledger = Checklist(path)
        self.links = TrackingLinks(self.ledger)
        self.monday = monday or MondayFollowup()
        self.source = source
        self.sender = sender or FollowupSender()
        self.lock = asyncio.Lock()
        self.state = {'connected':False, 'lastSuccess':None, 'sendingEnabled':True,
                      'historyCompletenessEnabled':True}
        self.last_log = None
        self.probe_done = False
        self.history_schema_logged = False
        self.automatic=None
        if AUTO_SELECT:
            from automatic_followup import AutomaticSelection
            self.automatic=AutomaticSelection(self)
        self.ledger.db.executescript('''CREATE TABLE IF NOT EXISTS checklist_context(
            id TEXT PRIMARY KEY,stage TEXT NOT NULL,email TEXT NOT NULL,kinds TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS checklist_baseline(form_id TEXT PRIMARY KEY,baseline INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS checklist_review(id TEXT PRIMARY KEY,reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS checklist_attempt(
                id INTEGER PRIMARY KEY AUTOINCREMENT,candidate_id TEXT NOT NULL,
                attempted_at TEXT NOT NULL,status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS history_analysis(
                candidate_id TEXT PRIMARY KEY,submission_id TEXT NOT NULL,digest TEXT,
                status TEXT NOT NULL,issues TEXT NOT NULL,anchor TEXT,review_reason TEXT);
            CREATE TABLE IF NOT EXISTS history_receipt_owner(
                submission_id TEXT PRIMARY KEY,candidate_id TEXT NOT NULL);''')
        # Legacy held campaigns have no attempt journal. No tracking links for
        # a requested form proves the old sender could not have been reached:
        # every form link was issued before its first reservation. Other legacy
        # holds remain uncertain rather than assuming zero confirmed sends means
        # no delivery attempt.
        for row in self.ledger.db.execute("SELECT c.*,x.kinds FROM checklist_candidate c JOIN checklist_context x ON c.id=x.id WHERE c.state='staff_review'").fetchall():
            known = self.ledger.db.execute('SELECT 1 FROM checklist_attempt WHERE candidate_id=?',(row['id'],)).fetchone()
            linked = self.ledger.db.execute('SELECT 1 FROM checklist_links WHERE candidate_id=?',(row['id'],)).fetchone()
            if not known and not (row['sends']==0 and not linked and set(json.loads(row['kinds'])) & FORMS.keys()):
                self.ledger.db.execute('INSERT INTO checklist_attempt(candidate_id,attempted_at,status) VALUES(?,?,?)',
                    (row['id'],datetime.now(timezone.utc).isoformat(),'unknown'))
        # An interrupted reservation is uncertain, even if the provider accepted it.
        for row in self.ledger.db.execute("SELECT id FROM checklist_candidate WHERE state='send_pending'").fetchall():
            self.ledger.db.execute('INSERT INTO checklist_attempt(candidate_id,attempted_at,status) VALUES(?,?,?)',
                (row['id'],datetime.now(timezone.utc).isoformat(),'unknown'))
        self.ledger.db.execute("UPDATE checklist_candidate SET state='staff_review' WHERE state='send_pending'")
        self.ledger.db.commit()
        self.audit = None
        if os.getenv('HR_PULSE_AUDIT','').lower() == 'true':
            from application_intake import PulseAudit
            self.audit = PulseAudit(self)

    def hold(self,item_id,reason):
        self.ledger.hold(item_id)
        with self.ledger.db:
            self.ledger.db.execute('INSERT OR REPLACE INTO checklist_review VALUES(?,?)',(item_id,reason))

    def review(self,item_id):
        campaign = self.ledger.db.execute('SELECT state,sends FROM checklist_candidate WHERE id=?',(item_id,)).fetchone()
        reason = self.ledger.db.execute('SELECT reason FROM checklist_review WHERE id=?',(item_id,)).fetchone()
        assessed=self.ledger.db.execute('SELECT status FROM history_analysis WHERE candidate_id=?',(item_id,)).fetchone()
        if assessed and assessed['status']=='staff_review':
            raise ValueError('History completeness needs HR review before reminders can resume.')
        attempts = dict(self.ledger.db.execute('SELECT status,count(*) FROM checklist_attempt WHERE candidate_id=? GROUP BY status',(item_id,)).fetchall())
        return {'itemId':item_id,'state':campaign['state'] if campaign else 'not_enrolled',
                'confirmedEmails':campaign['sends'] if campaign else 0,
                'reason':reason['reason'] if reason else 'Review current stage, email and selected items.',
                'deliveryUncertain':bool(attempts.get('unknown') or attempts.get('reserved')),
                'attemptedEmails':sum(attempts.values())}

    async def resume(self,item,now):
        item_id,stage,email,kinds,receipts,_ = identity(item)
        # Consume the request before changing durable state: failed clear means
        # no resume. An old tick can never restart a future pause.
        await self.monday.clear_resume(item_id)
        review = self.review(item_id)
        if review['deliveryUncertain']:
            raise ValueError('Delivery outcome uncertain; do not resend. Staff must resolve the send record.')
        if review['state'] in {'received_review','send_pending'} or review['confirmedEmails'] >= 4:
            raise ValueError('Completed or exhausted campaign cannot be reopened.')
        reason = self.ledger.db.execute('SELECT reason FROM checklist_review WHERE id=?',(item_id,)).fetchone()
        if reason and reason['reason']=='Linked form response needs staff review.':
            raise ValueError('Linked form response needs staff review.')
        context = self.ledger.db.execute('SELECT * FROM checklist_context WHERE id=?',(item_id,)).fetchone()
        if not context:
            return  # Valid explicit selection can now enroll normally.
        old = set(json.loads(context['kinds']))
        if not kinds or not old <= kinds or (kinds != old and review['confirmedEmails']):
            raise ValueError('Restore the original requested items before resuming.')
        with self.ledger.db:
            for kind in kinds-old:
                self.ledger.db.execute('INSERT OR IGNORE INTO checklist_requirement VALUES(?,?,?,NULL)',(item_id,kind,'awaiting'))
            self.ledger.db.execute('UPDATE checklist_context SET stage=?,email=?,kinds=? WHERE id=?',
                (stage,email,json.dumps(sorted(kinds)),item_id))
            waiting = self.ledger.db.execute("SELECT 1 FROM checklist_requirement WHERE candidate_id=? AND state='awaiting'",(item_id,)).fetchone()
            self.ledger.db.execute('UPDATE checklist_candidate SET state=? WHERE id=? AND state=?',
                ('active' if waiting else 'received_review',item_id,'staff_review'))
            self.ledger.db.execute('DELETE FROM checklist_review WHERE id=?',(item_id,))

    async def scan(self, source, schemas):
        # All pages are fetched before processing or sending any campaign.
        batches = {}
        for kind, form_id in FORMS.items():
            baseline = self.ledger.db.execute('SELECT baseline FROM checklist_baseline WHERE form_id=?',(form_id,)).fetchone()
            if not baseline:
                rows = await source.page(form_id, limit=1)
                if rows and (str(rows[0].get('form_id')) != form_id or not str(rows[0].get('id','')).isdigit()):
                    raise RuntimeError('Baseline identity mismatch')
                with self.ledger.db:
                    self.ledger.db.execute('INSERT INTO checklist_baseline VALUES(?,?)',
                        (form_id,int(rows[0]['id']) if rows else 0))
                batches[kind] = []
            else:
                batches[kind] = await source.after(form_id,baseline['baseline'])
        history_latest = {}
        for kind, rows in batches.items():
            for row in rows:
                if row.get('status') != 'ACTIVE':
                    continue
                answers = row.get('answers')
                if isinstance(answers,dict):
                    row = dict(row,answers={qid:dict(a,qid=qid) for qid,a in answers.items() if isinstance(a,dict)})
                try:
                    token = field(row.get('answers'),TRACKING).get('answer')
                except ReceiptHold:
                    continue
                link = self.ledger.db.execute('SELECT candidate_id,kind FROM checklist_links WHERE token=?',
                    (token if isinstance(token,str) else '',)).fetchone()
                if not link:
                    continue
                if kind=='history':
                    from recruitment_tracking import tracking_field
                    tracking = field(row.get('answers'),TRACKING)
                    q = tracking_field(schemas[kind])
                    if (link['kind'] != kind or str(row.get('form_id')) != FORMS[kind]
                            or tracking.get('type') != 'control_textbox' or str(tracking.get('qid')) != str(q.get('qid'))):
                        self.hold(link['candidate_id'],'Linked form response needs staff review.')
                        continue
                    previous=history_latest.get(link['candidate_id'])
                    if previous is None or int(row['id'])>int(previous['id']):
                        history_latest[link['candidate_id']]=row
                    continue
                try:
                    self.links.accept(schemas[kind],row)
                except (ReceiptHold,ValueError):
                    self.hold(link['candidate_id'],'Linked form response needs staff review.')
        for candidate,row in history_latest.items():
            self.history_result(candidate,schemas['history'],row)

    def history_result(self,candidate,schema,record):
        owner=self.ledger.db.execute('SELECT candidate_id FROM history_receipt_owner WHERE submission_id=?',(str(record['id']),)).fetchone()
        if owner and owner['candidate_id']!=candidate:
            self.hold(candidate,'Linked form response needs staff review.');return
        result=analyse_history(schema,record,datetime.now(ZoneInfo('Europe/London')).date())
        old=self.ledger.db.execute('SELECT * FROM history_analysis WHERE candidate_id=?',(candidate,)).fetchone()
        if old and int(old['submission_id'])>int(record['id']):
            self.hold(candidate,'Linked form response needs staff review.');return
        if (old and old['submission_id']==str(record['id']) and old['digest']==result['digest']
                and old['status']==result['status']):
            return
        owner=self.ledger.db.execute("SELECT candidate_id FROM checklist_requirement WHERE kind='history' AND evidence_id=?",(str(record['id']),)).fetchone()
        if owner and owner['candidate_id']!=candidate:
            self.hold(candidate,'Linked form response needs staff review.');return
        with self.ledger.db:
            self.ledger.db.execute('INSERT OR IGNORE INTO history_receipt_owner VALUES(?,?)',(str(record['id']),candidate))
            self.ledger.db.execute('INSERT OR REPLACE INTO history_analysis VALUES(?,?,?,?,?,?,?)',
                (candidate,str(record['id']),result['digest'],result['status'],json.dumps(result['issues']),result['anchor'],result['reviewReason']))
            if result['status']=='needs_information':
                self.ledger.db.execute("UPDATE checklist_requirement SET state='awaiting',evidence_id=? WHERE candidate_id=? AND kind='history'",
                    (str(record['id']),candidate))
                self.ledger.db.execute("UPDATE checklist_candidate SET state='active' WHERE id=? AND state='received_review'",(candidate,))
            elif result['status']=='complete':
                self.ledger.receive(candidate,'history',str(record['id']),exact_match=True)
        if result['status']=='staff_review':
            self.hold(candidate,'History completeness needs HR review: '+result['reviewReason'])
        log.info('History completeness assessed: item=%s status=%s missing=%d',candidate,result['status'],len(result['issues']))

    def reconcile(self, item, now, enroll=False):
        item_id, stage, email, kinds, receipts, _ = identity(item)
        context = self.ledger.db.execute('SELECT * FROM checklist_context WHERE id=?',(item_id,)).fetchone()
        if not context:
            if not enroll or not kinds:
                raise ValueError('Explicit request required')
            self.ledger.request(item_id,kinds,now,verified_link=True,staff_requested=True)
            with self.ledger.db:
                self.ledger.db.execute('INSERT INTO checklist_context VALUES(?,?,?,?)',
                    (item_id,stage,email,json.dumps(sorted(kinds))))
            from reconciliation_notices import apply_budget
            apply_budget(self,item_id,now)
        elif context['stage'] != stage or context['email'] != email or set(json.loads(context['kinds'])) != kinds:
            old_kinds = set(json.loads(context['kinds']))
            campaign = self.ledger.db.execute('SELECT state,sends FROM checklist_candidate WHERE id=?',(item_id,)).fetchone()
            attempts = self.ledger.db.execute(
                "SELECT count(*) FROM checklist_attempt WHERE candidate_id=? AND status IN ('unknown','reserved')",(item_id,)).fetchone()[0]
            reason = self.ledger.db.execute('SELECT reason FROM checklist_review WHERE id=?',(item_id,)).fetchone()
            safe_expansion = (context['stage'] == stage and context['email'] == email
                              and old_kinds < kinds and campaign and campaign['sends'] == 0
                              and campaign['state'] in {'active','staff_review'} and not attempts
                              and (campaign['state'] != 'staff_review' or
                                   (reason and reason['reason'] ==
                                    'Email, stage or requested items changed. Verify them, then tick HR follow-up resume.')))
            if safe_expansion:
                # No message has been accepted: expand this existing campaign, retaining
                # its send count and clock. Routine HR review must not block collection.
                with self.ledger.db:
                    for kind in kinds-old_kinds:
                        self.ledger.db.execute(
                            'INSERT OR IGNORE INTO checklist_requirement VALUES(?,?,?,NULL)',
                            (item_id,kind,'awaiting'))
                    self.ledger.db.execute('UPDATE checklist_context SET kinds=? WHERE id=?',
                                           (json.dumps(sorted(kinds)),item_id))
                    if campaign['state'] == 'staff_review':
                        self.ledger.db.execute(
                            "UPDATE checklist_candidate SET state='active' WHERE id=? AND state='staff_review'",(item_id,))
                        self.ledger.db.execute('DELETE FROM checklist_review WHERE id=?',(item_id,))
            else:
                self.hold(item_id,'Email, stage or requested items changed. Verify them, then tick HR follow-up resume.')
                raise ValueError('Campaign identity or request changed; staff review required')
        for kind in receipts & kinds:
            if kind=='history':
                assessed=self.ledger.db.execute('SELECT status FROM history_analysis WHERE candidate_id=?',(item_id,)).fetchone()
                if assessed and assessed['status']!='complete':
                    continue  # Receipt alone cannot override an incomplete history.
            self.ledger.receive(item_id,kind,item_id,exact_match=True)
        return item_id, stage, email

    def progress(self,item_id):
        campaign = self.ledger.db.execute('SELECT * FROM checklist_candidate WHERE id=?',(item_id,)).fetchone()
        review = self.review(item_id)
        if not campaign:
            return 'Follow-up paused: ' + review['reason'] + ' No campaign enrolled; no email attempted.'
        rows = list(self.ledger.db.execute('SELECT kind,state FROM checklist_requirement WHERE candidate_id=? ORDER BY kind',(item_id,)))
        waiting = [dict(LABELS.values())[r['kind']] for r in rows if r['state']=='awaiting']
        count = len(rows)-len(waiting)
        status = {'received_review':'All requested information received; reminders stopped. Staff review remains required.',
                  'staff_review':'Reminders paused; HR/recruitment review required.',
                  'active':'Awaiting information.', 'send_pending':'Delivery reserved; do not resend.'}[campaign['state']]
        if campaign['state']=='staff_review':
            status += ' Reason: ' + review['reason'] + (' Delivery is uncertain; resume blocked.' if review['deliveryUncertain'] else ' Tick HR follow-up resume after correcting the issue.')
        history=self.ledger.db.execute('SELECT status,issues,review_reason FROM history_analysis WHERE candidate_id=?',(item_id,)).fetchone()
        if history:
            if history['status']=='complete':
                status += ' History administratively complete; HR verification remains required.'
            elif history['status']=='needs_information':
                status += ' History received but incomplete: ' + ' '.join(json.loads(history['issues']))
            else:
                status += ' History needs HR review: '+history['review_reason']+'.'
        return status + ' Received: %d/%d. Emails: %d/4.' % (count,len(rows),campaign['sends']) + (
            ' Outstanding: ' + ', '.join(waiting) + '.' if waiting else '') + (
            ' Next reminder: ' + campaign['next_due'] + '.' if campaign['state']=='active' and campaign['sends'] else '')

    async def run(self, now=None):
        async with self.lock:
            now = now or datetime.now(timezone.utc)
            phase = 'board_schema'
            try:
                source = self.source or WorkflowSource(os.getenv('JOTFORM_RECRUITMENT_READ_API_KEY',''))
                await self.monday.schema()
                phase = 'source_schema'
                schemas = {kind:await source.schema(form_id) for kind,form_id in FORMS.items()}
                check_history_schema(schemas['history'])
                if not self.history_schema_logged:
                    log.info('Staff history completeness schema verified: format=configurable_list')
                    self.history_schema_logged=True
                # Validate schema before baseline establishment and enrollment.
                from recruitment_tracking import tracking_field
                for kind, schema in schemas.items():
                    tracking_field(schema)
                    if field(schema,SIGNATURES[kind]).get('type') != 'control_signature':
                        raise RuntimeError('Signature schema changed')
                choice = field(schemas['working_time'],CHOICE)
                if choice.get('type') not in {'control_radio','control_dropdown'} or not isinstance(choice.get('options'),str) or not choice['options']:
                    raise RuntimeError('Working-time schema changed')
                phase = 'receipt_scan'
                await self.scan(source,schemas)
                if self.automatic:
                    phase='automatic_selection'
                    await self.automatic.run(source,now)
                phase = 'board_progress_probe'
                if not self.probe_done:
                    # Previously authorised Andy trial: only the new progress field.
                    probe = await self.monday.item('1005891310')
                    probe_id = str(probe.get('id',''))
                    cols = {c['id']:c for c in probe['column_values']}
                    if (probe_id != '1005891310' or str(probe.get('board',{}).get('id')) != BOARD
                            or probe.get('state') != 'active' or probe.get('group',{}).get('id') != 'topics'
                            or cols['status53']['text'] != 'Director'):
                        raise RuntimeError('Agreed progress probe record changed')
                    selected, old = selections(cols[REQUEST]), cols[SUMMARY].get('text')
                    if not selected:
                        note = 'Follow-up connection verified. No items requested; no reminder sent.'
                        if old != note:
                            await self.monday.summary(probe_id,note)
                        self.state['boardProgressVerified'] = True
                    self.probe_done = True
                phase = 'request_scan'
                requested = await self.monday.requested()
                for item in requested:
                    if self.automatic and self.automatic.intake and self.automatic.intake.owned(item.get('id')):
                        continue  # Intake has its own source matching, budget and durable send journal.
                    try:
                        controls = {c['id']:c for c in item['column_values']}
                        action = json.loads(controls.get(RESUME,{}).get('value') or '{}')
                        if str(action.get('checked','')).lower()=='true':
                            await self.resume(item,now)
                        self.reconcile(item,now,enroll=True)
                    except (ValueError,KeyError,TypeError) as exc:
                        item_id = str(item.get('id',''))
                        if item_id.isdigit():
                            safe = {'Verified single staff email required':'Email missing or invalid. Set the Email field to the verified recipient.',
                                    'Employment stage requires staff review':'Employment stage and group need staff review.',
                                    'Medical request stage requires staff review':'Confirm the approved stage for requesting the medical form.',
                                    'Delivery outcome uncertain; do not resend. Staff must resolve the send record.':'Delivery outcome uncertain; do not resend. Staff must resolve the send record.',
                                    'Completed or exhausted campaign cannot be reopened.':'Completed or exhausted campaign cannot be reopened.',
                                    'Restore the original requested items before resuming.':'Restore the original requested items before resuming.',
                                    'Linked form response needs staff review.':'Linked form response needs staff review.'}
                            safe['Select the single approved contract for this employee.']='Select the single approved contract for this employee.'
                            safe['Automatic follow-up paused for this person.']='Automatic follow-up paused for this person. Untick the pause control before using resume.'
                            safe['History completeness needs HR review before reminders can resume.']='History completeness needs HR review before reminders can resume.'
                            reason = safe.get(str(exc),'Verify stage, email and requested items, then tick HR follow-up resume.')
                            existing = self.ledger.db.execute('SELECT state FROM checklist_candidate WHERE id=?',(item_id,)).fetchone()
                            if not existing or existing['state'] != 'received_review':
                                self.hold(item_id,reason)
                            await self.monday.summary(item_id,self.progress(item_id))
                phase = 'campaigns'
                ids = [r['id'] for r in self.ledger.db.execute('SELECT id FROM checklist_context')]
                for item_id in ids:
                    try:
                        item = await self.monday.item(item_id)
                        # A manual pause must stop an already-enrolled campaign too.
                        # Do not release this hold automatically when the pause is unticked.
                        columns = {c['id']:c for c in item['column_values']}
                        paused = str(json.loads(columns.get(AUTO_PAUSE,{}).get('value') or '{}').get('checked','')).lower() == 'true'
                        if paused:
                            self.hold(item_id,'Automatic follow-up paused for this person.')
                            await self.monday.summary(item_id,self.progress(item_id))
                            continue
                        _, stage, recipient = self.reconcile(item,now)
                        kinds = self.ledger.plan(item_id,now)
                        if kinds:
                            links = {kind:self.links.issue(item_id,kind,schemas[kind]) for kind in kinds if kind in FORMS}
                            # Fresh canonical receipt scan and staff routing immediately before send.
                            await self.scan(source,schemas)
                            item = await self.monday.item(item_id)
                            self.reconcile(item,now)
                            kinds = self.ledger.reserve(item_id,now)
                            if kinds:
                                with self.ledger.db:
                                    attempt = self.ledger.db.execute('INSERT INTO checklist_attempt(candidate_id,attempted_at,status) VALUES(?,?,?)',
                                        (item_id,now.isoformat(),'reserved')).lastrowid
                                try:
                                    await self.sender.send(stage,recipient,'ESCS outstanding staff information' if stage=='employed'
                                        else 'ESCS outstanding recruitment information',render(kinds,stage,{k:v for k,v in links.items() if k in kinds},
                                            self.history_issues(item_id)))
                                    self.ledger.delivery(item_id,now,confirmed=True)
                                    with self.ledger.db:
                                        self.ledger.db.execute("UPDATE checklist_attempt SET status='accepted' WHERE id=?",(attempt,))
                                    log.info('Staff follow-up delivery accepted: item=%s',item_id)
                                except Exception:
                                    self.ledger.delivery(item_id,now,confirmed=False)
                                    with self.ledger.db:
                                        self.ledger.db.execute("UPDATE checklist_attempt SET status='unknown' WHERE id=?",(attempt,))
                                    self.hold(item_id,'Delivery outcome uncertain; do not resend. Staff must resolve the send record.')
                                    log.info('Staff follow-up delivery uncertain: item=%s automaticRetry=false',item_id)
                        update = self.progress(item_id)
                        if identity(item)[5] != update:
                            await self.monday.summary(item_id,update)
                        if self.audit:
                            self.audit.snapshot(item_id,'hr-followup',update)
                    except Exception:
                        self.hold(item_id,'Staff record or receipt lookup failed. Verify stage, email and requested items before resuming.')
                        # No personal data or provider errors are logged/exposed.
                        try:
                            await self.monday.summary(item_id,self.progress(item_id))
                            if self.audit:
                                self.audit.snapshot(item_id,'hr-followup',self.progress(item_id))
                        except Exception:
                            pass
                counts = dict(self.ledger.db.execute('SELECT state,count(*) FROM checklist_candidate GROUP BY state').fetchall())
                self.state.update(connected=True,lastSuccess=now.isoformat(),campaigns=sum(counts.values()),
                    active=counts.get('active',0),received=counts.get('received_review',0),review=counts.get('staff_review',0),error=None)
                line = 'Staff follow-up workflow connected: campaigns=%d active=%d received=%d review=%d sending=true' % (
                    self.state['campaigns'],self.state['active'],self.state['received'],self.state['review'])
            except Exception:
                self.state.update(connected=False,error='follow_up_connection_requires_review',failedPhase=phase)
                line = 'Staff follow-up workflow unavailable: phase=' + phase + '; this cycle blocked before further sending'
            if line != self.last_log:
                log.info(line)
                self.last_log = line
            if self.audit:
                await self.audit.flush()
            return dict(self.state)

    async def loop(self):
        while True:
            await self.run()
            await asyncio.sleep(300)

    def history_issues(self,item_id):
        row=self.ledger.db.execute("SELECT issues FROM history_analysis WHERE candidate_id=? AND status='needs_information'",(item_id,)).fetchone()
        return json.loads(row['issues']) if row else []


def build_workflow_router(worker, require_key):
    router = APIRouter(prefix='/recruitment-checklist/workflow',dependencies=[Depends(require_key)])
    @router.get('/status')
    async def status():
        return dict(worker.state)
    @router.get('/reviews')
    async def reviews():
        return {'reviews':[worker.review(r['id']) for r in worker.ledger.db.execute('SELECT id FROM checklist_review ORDER BY id')]}
    return router
