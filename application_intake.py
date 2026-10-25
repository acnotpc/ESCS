"""Administrative application intake; receipt never means clearance or acceptance."""
import html
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from automatic_followup import values, text, data, names, normalized, PAUSE
from recruitment_receipts import present

ENABLED = os.getenv('HR_APPLICATION_INTAKE', '').lower() == 'true'
START = '2026-10-25T13:00:00+00:00'
FORMS = {
    'dbs': ('92583319416361', 'New starter checklist', 5),
    'history': ('211923425771051', '10-year employment history', 1),
    'working_time': ('82551787411460', '48-hour working-time choice', 4),
    'licence_details': ('73002266184349', 'Driving licence check information', 14),
    'driving_declaration': ('223043594296359', 'Driving declaration', 11),
}
NAME_TITLES = {'name', 'name of employee', 'applicant name'}
EMAIL_TITLES = {'email', 'e-mail', 'personal email address', 'email address', 'applicant email'}


def dob(value):
    try:
        if isinstance(value, dict):
            return datetime(int(value['year']), int(value['month']), int(value['day'])).date().isoformat()
        return datetime.fromisoformat(value).date().isoformat()
    except (ValueError, TypeError, KeyError):
        return ''


class ApplicationIntake:
    def __init__(self, automatic):
        self.auto = automatic
        self.worker = automatic.worker
        self.db = self.worker.ledger.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS application_intake(
                id TEXT PRIMARY KEY, application_id TEXT NOT NULL, started TEXT NOT NULL,
                state TEXT NOT NULL, sends INTEGER NOT NULL DEFAULT 0, due TEXT NOT NULL,
                identity_hash TEXT NOT NULL, receipt_ids TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS intake_archive(
                kind TEXT NOT NULL, sid TEXT NOT NULL, typ TEXT NOT NULL, digest TEXT NOT NULL,
                PRIMARY KEY(kind,sid,typ,digest));
            CREATE TABLE IF NOT EXISTS intake_source(kind TEXT PRIMARY KEY, watermark INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS intake_empty_submission(
                kind TEXT NOT NULL, sid TEXT NOT NULL, PRIMARY KEY(kind,sid));
            CREATE TABLE IF NOT EXISTS intake_attempt(
                id INTEGER PRIMARY KEY AUTOINCREMENT, candidate TEXT NOT NULL,
                attempted TEXT NOT NULL, state TEXT NOT NULL);
        ''')
        if 'controls_set' not in {r['name'] for r in self.db.execute('PRAGMA table_info(application_intake)')}:
            self.db.execute('ALTER TABLE application_intake ADD COLUMN controls_set INTEGER NOT NULL DEFAULT 0')
        # An interrupted send is not safe to retry.
        with self.db:
            self.db.execute("UPDATE application_intake SET state='delivery_uncertain' WHERE state='sending'")
        self.state = {}

    def owned(self, item_id):
        return bool(self.db.execute('SELECT 1 FROM application_intake WHERE id=?', (str(item_id),)).fetchone())

    def keys(self, answers):
        if not isinstance(answers, dict):
            raise ValueError('Receipt answers unavailable')
        keys = set()
        full_names = [a for a in answers.values() if isinstance(a, dict) and a.get('type') == 'control_fullname']
        for a in answers.values():
            if not isinstance(a, dict):
                raise ValueError('Receipt schema changed')
            title = normalized(a.get('text', '')).rstrip(':')
            value = a.get('answer')
            if title in NAME_TITLES or (len(full_names) == 1 and a is full_names[0]):
                keys.update(('name', self.auto.index.digest(n)) for n in names(value))
            elif title in EMAIL_TITLES and isinstance(value, str) and '@' in value:
                keys.add(('email', self.auto.index.digest(normalized(value))))
            elif title == 'date of birth' and dob(value):
                keys.add(('dob', self.auto.index.digest(dob(value))))
        # Legacy licence responses can omit the name but retain an email. Keep
        # that partial key: it holds the matching person for review, never counts
        # as a verified receipt and need not block unrelated applicants.
        if not keys:
            raise ValueError('Receipt name unavailable')
        return keys

    async def refresh(self, source, kind, form):
        old = self.db.execute('SELECT watermark FROM intake_source WHERE kind=?', (kind,)).fetchone()
        watermark = old['watermark'] if old else 0
        largest = watermark
        rows = []
        empty = []
        seen = set()
        previous = None
        for offset in range(0, 50000, 100):
            page = await source._get('/form/' + form + '/submissions?limit=100&offset=' + str(offset) + '&orderby=id&direction=DESC')
            if not isinstance(page, list):
                raise ValueError('Incomplete intake source')
            ids = tuple(str(r.get('id')) for r in page)
            if page and ids in seen:
                raise ValueError('Repeated intake page')
            seen.add(ids)
            reached = False
            for row in page:
                if str(row.get('form_id')) != form or not str(row.get('id', '')).isdigit():
                    raise ValueError('Intake source identity changed')
                sid = int(row['id'])
                if previous is not None and sid > previous:
                    raise ValueError('Receipt ordering changed')
                previous = sid
                largest = max(largest, sid)
                if sid <= watermark:
                    reached = True
                    continue
                if row.get('status') == 'ACTIVE':
                    answers = row.get('answers')
                    # Historical schema-only stubs contain no submitted answers.
                    # Preserve their IDs for review; they cannot be a returned form.
                    if (isinstance(answers, dict) and answers
                            and all(isinstance(a, dict) for a in answers.values())
                            and not any(present(a.get('answer')) for a in answers.values())):
                        empty.append((kind, str(sid)))
                        continue
                    try:
                        keys = self.keys(answers)
                    except ValueError:
                        self.failed_submission = str(sid)
                        raise
                    rows.extend((kind, str(sid), typ, digest) for typ, digest in keys)
            if reached or len(page) < 100:
                break
        else:
            raise ValueError('Incomplete intake archive')
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO intake_archive VALUES(?,?,?,?)', rows)
            self.db.executemany('INSERT OR IGNORE INTO intake_empty_submission VALUES(?,?)', empty)
            self.db.execute('INSERT OR REPLACE INTO intake_source VALUES(?,?)', (kind, largest))

    def matching(self, kind, candidate_keys):
        matches = {}
        for typ, digest in candidate_keys:
            for row in self.db.execute('SELECT sid,typ FROM intake_archive WHERE kind=? AND typ=? AND digest=?', (kind, typ, digest)):
                matches.setdefault(row['sid'], set()).add(typ)
        exact = [sid for sid, types in matches.items() if 'name' in types and ({'email', 'dob'} & types)]
        if exact:
            return max(exact, key=int), False
        # A partial identity match suppresses further mail pending a human check.
        return None, any(types & {'name', 'email'} for types in matches.values())

    async def write(self, item_id, writes):
        result = await self.worker.monday.call(
            'mutation($item:ID!,$value:JSON!){change_multiple_column_values(board_id:424237712,item_id:$item,column_values:$value){id}}',
            {'item':item_id, 'value':json.dumps(writes)})
        if str(result['change_multiple_column_values']['id']) != item_id:
            raise ValueError('Intake update unconfirmed')

    async def run(self, source, roster, counts, name_counts, now):
        from checklist_workflow import REQUEST, RECEIVED, SUMMARY
        phase = 'application_archive'
        self.failed_submission = None
        try:
            # Application identity plus all requested archives must read successfully.
            await self.refresh(source, 'application', '260903309464356')
            for kind, (form, _, _) in FORMS.items():
                phase = kind + '_archive'
                await self.refresh(source, kind, form)
            phase = 'records'
            enrolled = active = completed = held = 0
            for item in roster:
                item_id = str(item['id'])
                cols = values(item)
                existing = self.db.execute('SELECT * FROM application_intake WHERE id=?', (item_id,)).fetchone()
                if not existing:
                    created = datetime.fromisoformat(item.get('created_at', '').replace('Z', '+00:00')) if item.get('created_at') else None
                    if not created or created < datetime.fromisoformat(START):
                        continue
                    if (item.get('group', {}).get('id') != 'new_group' or text(cols, 'status53') != 'Pending Review'
                            or data(cols, REQUEST).get('ids') or data(cols, RECEIVED).get('ids')
                            or data(cols, PAUSE).get('checked') in {True, 'true'}):
                        continue
                    if self.db.execute('SELECT 1 FROM checklist_candidate WHERE id=?', (item_id,)).fetchone():
                        continue
                email = normalized(data(cols, 'email').get('email', ''))
                if (not re.fullmatch(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+', email)
                        or email in {'jobs@securecareservices.co.uk', 'hr@securecareservices.co.uk'}
                        or not names(item.get('name', '')) or counts.get(email) != 1
                        or any(name_counts.get(n) != 1 for n in names(item.get('name', '')))):
                    held += 1
                    if existing and self.worker.audit:
                        self.worker.audit.snapshot(item_id, 'intake', 'Reminders held: applicant identity/email is missing, invalid or duplicated. Recruitment review required.')
                    continue
                keys = {('name', self.auto.index.digest(n)) for n in names(item.get('name', ''))}
                keys.add(('email', self.auto.index.digest(email)))
                birthday = data(cols, 'date6').get('date')
                if dob(birthday):
                    keys.add(('dob', self.auto.index.digest(dob(birthday))))
                identity_hash = self.auto.index.digest(json.dumps(sorted(keys)))
                application, uncertain = self.matching('application', keys)
                if not application or uncertain:
                    held += 1
                    if existing and self.worker.audit:
                        self.worker.audit.snapshot(item_id, 'intake', 'Reminders held: the application-to-person link requires Recruitment review.')
                    continue
                if not existing:
                    fresh = await self.auto.fresh(item_id)
                    if fresh != item:
                        continue
                    with self.db:
                        self.db.execute('INSERT INTO application_intake(id,application_id,started,state,due,identity_hash) VALUES(?,?,?,?,?,?)',
                            (item_id, application, created.isoformat(), 'enrolling', (created + timedelta(hours=72)).isoformat(), identity_hash))
                    existing = self.db.execute('SELECT * FROM application_intake WHERE id=?', (item_id,)).fetchone()
                    enrolled += 1
                # Recheck contact/stage and pause before every mutation or send.
                fresh = await self.auto.fresh(item_id)
                if fresh != item:
                    continue
                group = item.get('group', {}).get('id')
                blocked = (existing['identity_hash'] != identity_hash or
                           data(cols, PAUSE).get('checked') in {True, 'true'} or
                           text(cols, 'acceptance') in {'No', 'Sent Closed Email', 'Sent closing email'} or
                           ((group, text(cols, 'status53')) not in {
                               ('new_group', 'Pending Review'), ('new_group88715', 'To be processed by Recruitment')}
                            and not (existing['state'] == 'enrolling' and group == 'new_group'
                                     and text(cols, 'status53') == 'To be processed by Recruitment')))
                if existing['state'] == 'enrolling' and not blocked:
                    # Administrative queue movement only. Acceptance is deliberately unchanged.
                    await self.write(item_id, {'status53':{'label':'To be processed by Recruitment'},
                        'offerletter_new_starter_jotform':{'date':datetime.fromisoformat(existing['started']).astimezone(ZoneInfo('Europe/London')).date().isoformat()},
                        'long_text_mm81znz1':{'text':'Application-stage tracking enrolled. Initial links are in the Jotform acknowledgement; do not resend the initial pack. Check the mobile-number reply and review returned forms. Receipt does not authorise employment. Previous review note: ' + text(cols, 'long_text_mm81znz1')}})
                    moved = await self.worker.monday.call('mutation($item:ID!){move_item_to_group(item_id:$item,group_id:"new_group88715"){id}}', {'item':item_id})
                    if str(moved['move_item_to_group']['id']) != item_id:
                        raise ValueError('Intake move unconfirmed')
                    with self.db:
                        self.db.execute("UPDATE application_intake SET state='active' WHERE id=?", (item_id,))
                    item = await self.auto.fresh(item_id)
                    cols = values(item)
                    if (item.get('group', {}).get('id') != 'new_group88715'
                            or text(cols, 'status53') != 'To be processed by Recruitment'):
                        raise ValueError('Intake move unconfirmed')
                    existing = self.db.execute('SELECT * FROM application_intake WHERE id=?', (item_id,)).fetchone()
                    if self.worker.audit:
                        self.worker.audit.event(item_id, 'intake-enrolled:' + item_id,
                            'Application tracking enrolled; moved into To be processed by Recruitment. Acceptance and employment clearance were not granted. The initial form links are supplied by the application autoresponder; inbox delivery is unverified.')
                receipts = json.loads(existing['receipt_ids'])
                kinds = set(FORMS)
                # The application declaration determines whether driving forms are relevant.
                if text(cols, 'color_mm2ffyt5') != 'Yes':
                    kinds -= {'licence_details', 'driving_declaration'}
                expected_ids = sorted(FORMS[k][2] for k in kinds)
                controls_changed = bool(existing['controls_set'] and data(cols, REQUEST).get('ids', []) != expected_ids)
                blocked |= controls_changed
                if existing['controls_set']:
                    for kind in kinds:
                        if FORMS[kind][2] in data(cols, RECEIVED).get('ids', []):
                            receipts.setdefault(kind, 'staff-confirmed')
                ambiguity = False
                history_issues = []
                for kind in kinds:
                    sid, partial = self.matching(kind, keys)
                    ambiguity |= partial
                    if sid:
                        # Re-read canonical evidence, not just the cached archive identity.
                        evidence = await source._get('/submission/' + sid)
                        if (not isinstance(evidence, dict) or str(evidence.get('id')) != sid
                                or str(evidence.get('form_id')) != FORMS[kind][0]
                                or evidence.get('status') != 'ACTIVE'):
                            raise ValueError('Canonical intake receipt unavailable')
                        shared_types = {typ for typ, digest in self.keys(evidence.get('answers')) if (typ, digest) in keys}
                        if 'name' not in shared_types or not shared_types & {'email', 'dob'}:
                            ambiguity = True
                            continue
                        if kind == 'history':
                            schema = await source.schema(FORMS[kind][0])
                            evidence = dict(evidence, answers={qid:dict(a,qid=qid) for qid,a in evidence['answers'].items()})
                            self.worker.history_result(item_id, schema, evidence)
                            analysis = self.db.execute('SELECT status,issues FROM history_analysis WHERE candidate_id=?', (item_id,)).fetchone()
                            if not analysis or analysis['status'] == 'staff_review':
                                ambiguity = True
                                continue
                            if analysis['status'] == 'needs_information':
                                receipts.pop(kind, None)
                                history_issues = json.loads(analysis['issues'])
                                continue
                        receipts[kind] = sid
                missing = kinds - receipts.keys()
                state = existing['state']
                if blocked:
                    state = 'paused'
                elif ambiguity:
                    state = 'identity_review'
                elif not missing:
                    state = 'received_review'
                elif existing['sends'] >= 3:
                    state = 'limit_review'
                with self.db:
                    self.db.execute('UPDATE application_intake SET state=?,receipt_ids=? WHERE id=?', (state, json.dumps(receipts), item_id))
                due = datetime.fromisoformat(existing['due'])
                note = ('Application checklist tracking: ' + str(len(kinds) - len(missing)) + '/' + str(len(kinds)) +
                        ' forms received. Initial links supplied by the application autoresponder; inbox delivery unverified. ')
                if state == 'active':
                    active += 1
                    note += 'Next reminder: ' + due.astimezone(ZoneInfo('Europe/London')).strftime('%d %B %Y %H:%M %Z') + '. '
                elif state == 'received_review':
                    completed += 1
                    note += 'All requested forms received; reminders stopped. Recruitment must check completeness and clear the next stage. '
                else:
                    held += 1
                    note += 'Reminders held: ' + state + '; Recruitment review required. '
                note += 'Reminder emails accepted: ' + str(existing['sends']) + '/3. Receipt is not approval; medical and reference permission remain separate.'
                if history_issues:
                    note += ' History clarification required: ' + '; '.join(history_issues)
                requested_ids = sorted(FORMS[k][2] for k in kinds)
                received_ids = sorted(FORMS[k][2] for k in kinds & receipts.keys())
                writes = {REQUEST:{'ids':requested_ids}, RECEIVED:{'ids':received_ids}, SUMMARY:{'text':note}}
                if controls_changed or blocked:
                    writes = {SUMMARY:{'text':note}}
                if (data(cols, REQUEST).get('ids') != requested_ids or data(cols, RECEIVED).get('ids') != received_ids
                        or text(cols, SUMMARY) != note):
                    await self.write(item_id, writes)
                if not blocked:
                    with self.db:
                        self.db.execute('UPDATE application_intake SET controls_set=1 WHERE id=?', (item_id,))
                if self.worker.audit:
                    self.worker.audit.snapshot(item_id, 'intake', note + '\nRequested: ' + ', '.join(FORMS[k][1] for k in sorted(kinds)) +
                        '\nReceived: ' + (', '.join(FORMS[k][1] for k in sorted(kinds & receipts.keys())) or 'None verified.'))
                if state != 'active' or now < due:
                    continue
                current = await self.auto.fresh(item_id)
                current_cols = values(current)
                if (current.get('name') != item.get('name') or current.get('group') != item.get('group')
                        or data(current_cols, 'email') != data(cols, 'email')
                        or text(current_cols, 'status53') != text(cols, 'status53')
                        or data(current_cols, PAUSE).get('checked') in {True, 'true'}
                        or data(current_cols, REQUEST).get('ids') != requested_ids
                        or data(current_cols, RECEIVED).get('ids') != received_ids):
                    continue
                # Complete archive scan above is required before every send. Reserve durably first.
                with self.db:
                    self.db.execute("UPDATE application_intake SET state='sending' WHERE id=? AND state='active'", (item_id,))
                    attempt = self.db.execute('INSERT INTO intake_attempt(candidate,attempted,state) VALUES(?,?,?)', (item_id, now.isoformat(), 'reserved')).lastrowid
                body = '<p>Dear applicant,</p><p>Following your ESCS application, we are awaiting these forms:</p><ul>'
                body += ''.join('<li><a href="https://form.jotform.com/' + FORMS[k][0] + '">' + html.escape(FORMS[k][1]) + '</a></li>' for k in sorted(missing))
                if history_issues:
                    body += '</ul><p>Your history has been received, but these details need clarification:</p><ul>'
                    body += ''.join('<li>' + html.escape(issue) + '</li>' for issue in history_issues)
                body += ('</ul><p>If you believe this information has already been submitted, please reply to this email so we can check our spam/junk folders and any recording errors.</p>'
                         '<p>Please also confirm your correct mobile number if the number on your application was incorrect. Do not email identity, medical or bank documents.</p>'
                         '<p>Kind regards,<br>Recruitment Team<br>Exclusive Secure Care Services</p>')
                try:
                    await self.worker.sender.send('pre_employment', email, 'ESCS application – outstanding forms', body)
                except Exception:
                    with self.db:
                        self.db.execute("UPDATE application_intake SET state='delivery_uncertain' WHERE id=?", (item_id,))
                        self.db.execute("UPDATE intake_attempt SET state='unknown' WHERE id=?", (attempt,))
                    if self.worker.audit:
                        self.worker.audit.event(item_id, 'intake-send:' + str(attempt),
                            'Reminder delivery outcome is uncertain. Automatic resend is blocked; Recruitment must reconcile the send record.')
                else:
                    with self.db:
                        self.db.execute("UPDATE application_intake SET state='active',sends=sends+1,due=? WHERE id=?", ((now + timedelta(hours=72)).isoformat(), item_id))
                        self.db.execute("UPDATE intake_attempt SET state='accepted' WHERE id=?", (attempt,))
                    if self.worker.audit:
                        self.worker.audit.event(item_id, 'intake-send:' + str(attempt),
                            'Reminder ' + str(existing['sends'] + 1) + '/3 accepted by the email service; inbox delivery is not established. Outstanding: ' +
                            ', '.join(FORMS[k][1] for k in sorted(missing)) + '. Next reminder, if still needed: ' +
                            (now + timedelta(hours=72)).astimezone(ZoneInfo('Europe/London')).strftime('%d %B %Y %H:%M %Z') + '.')
            empty_count = self.db.execute('SELECT count(*) FROM intake_empty_submission').fetchone()[0]
            self.state = {'enabled': True, 'lastSuccess':now.isoformat(), 'enrolled':enrolled, 'active':active, 'received':completed, 'held':held, 'emptyArchiveStubs':empty_count, 'error':None}
        except Exception as exc:
            self.state = {'enabled':True, 'error':'application_intake_requires_review', 'failedPhase':phase}
            safe_reasons = {'Receipt name unavailable', 'Receipt answers unavailable', 'Receipt schema changed',
                            'Incomplete intake source', 'Repeated intake page', 'Intake source identity changed',
                            'Receipt ordering changed', 'Incomplete intake archive', 'Canonical intake receipt unavailable',
                            'Intake update unconfirmed', 'Intake move unconfirmed'}
            self.state['reason'] = str(exc) if str(exc) in safe_reasons else type(exc).__name__
            if self.failed_submission:
                self.state['sourceSubmissionId'] = self.failed_submission
            if self.worker.audit:
                for row in self.db.execute('SELECT id FROM application_intake').fetchall():
                    self.worker.audit.snapshot(row['id'], 'intake', 'This intake cycle could not complete. Source/processing phase: ' + phase +
                        '. A failed lookup is not evidence that a form is missing. Check the current campaign and send journal before further action.')
        self.worker.state['applicationIntake'] = dict(self.state)
        import logging
        logging.getLogger('escs.automatic.followup').info('Application intake cycle: %s', json.dumps(self.state))
        return self.state


class PulseAudit:
    """Durable audit outbox. Uncertain posts are reconciled, never blindly resent."""
    def __init__(self, worker):
        self.worker = worker
        self.db = worker.ledger.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS pulse_audit_event(
                event_key TEXT PRIMARY KEY,item_id TEXT NOT NULL,body TEXT NOT NULL,
                state TEXT NOT NULL,update_id TEXT,priority INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS pulse_audit_snapshot(
                item_id TEXT NOT NULL,scope TEXT NOT NULL,fingerprint TEXT NOT NULL,
                version INTEGER NOT NULL,PRIMARY KEY(item_id,scope));
        ''')
        if 'priority' not in {r['name'] for r in self.db.execute('PRAGMA table_info(pulse_audit_event)')}:
            self.db.execute('ALTER TABLE pulse_audit_event ADD COLUMN priority INTEGER NOT NULL DEFAULT 1')
        with self.db:
            self.db.execute("UPDATE pulse_audit_event SET state='unknown' WHERE state='sending'")
        self.last_counts = None

    def event(self, item_id, key, message, _commit=True, _priority=0):
        if not str(item_id).isdigit():
            raise ValueError('Numeric recruitment pulse required')
        event_key = hashlib.sha256((str(item_id) + ':' + key).encode()).hexdigest()
        marker = 'ESCS-AUDIT-' + event_key[:24]
        when = datetime.now(ZoneInfo('Europe/London')).strftime('%d %B %Y %H:%M:%S %Z')
        body = ('<p><strong>ESCS automated recruitment audit</strong><br>Recorded ' + when + '</p><p>' +
                html.escape(message).replace('\n', '<br>') + '</p><p>Audit reference: ' + marker + '</p>')
        self.db.execute("INSERT OR IGNORE INTO pulse_audit_event(event_key,item_id,body,state,update_id,priority) VALUES(?,?,?,'pending',NULL,?)", (event_key, str(item_id), body, _priority))
        if _commit:
            self.db.commit()

    def snapshot(self, item_id, scope, message):
        fingerprint = hashlib.sha256(message.encode()).hexdigest()
        previous = self.db.execute('SELECT * FROM pulse_audit_snapshot WHERE item_id=? AND scope=?', (str(item_id), scope)).fetchone()
        if previous and previous['fingerprint'] == fingerprint:
            return
        version = previous['version'] + 1 if previous else 1
        # Commit the event and comparison checkpoint together.
        with self.db:
            self.event(item_id, scope + ':' + str(version) + ':' + fingerprint,
                ('Current verified workflow position (baseline; not a reconstruction of earlier events).\n' if not previous else 'Workflow position changed.\n') + message,
                _commit=False, _priority=2 if not previous and scope=='applicant-board' else 1)
            self.db.execute('INSERT OR REPLACE INTO pulse_audit_snapshot VALUES(?,?,?,?)', (str(item_id), scope, fingerprint, version))

    def applicants(self, roster):
        from checklist_workflow import PRE_GROUPS, REQUEST, RECEIVED
        for item in roster:
            if (item.get('state') != 'active' or str(item.get('board', {}).get('id')) != '424237712'
                    or item.get('group', {}).get('id') not in PRE_GROUPS | {'new_group'}):
                continue
            cols = values(item)
            status = text(cols, 'status53')
            if status in {'Leaver', 'Candidate Withdrawn', 'Rejected through process'}:
                continue
            # Intake posts richer form/receipt events; the common status snapshot
            # also captures human changes to the stage and acceptance controls.
            requested = text(cols, REQUEST) or 'None selected'
            received = text(cols, RECEIVED) or 'None recorded'
            message = ('Recorded recruitment stage: ' + (status or 'Not set') +
                       '\nRecorded acceptance: ' + (text(cols, 'acceptance') or 'Not set') +
                       '\nRecruitment owner: ' + (text(cols, 'multiple_person_mm81gba1') or 'Not assigned') +
                       '\nAction due: ' + (text(cols, 'date_mm819cpq') or 'Not set') +
                       '\nRequested follow-up: ' + requested + '\nRecorded received items: ' + received +
                       '\nThese are board records, not independent verification of evidence or authorisation to work.')
            checks = [('References received','status24'),('References confirmed','status9'),('10-year history','status7'),
                      ('Right to work','status583'),('DBS status','status3'),('Medical receipt','status72'),
                      ('Medical approval','status_mkmvm8rd'),('Contract status','status797')]
            message += '\nRecorded checks: ' + '; '.join(label + ': ' + (text(cols, col) or 'Not recorded') for label,col in checks)
            self.snapshot(str(item['id']), 'applicant-board', message)

    async def flush(self):
        for row in self.db.execute("SELECT * FROM pulse_audit_event WHERE state IN ('pending','unknown') ORDER BY CASE state WHEN 'pending' THEN 0 ELSE 1 END,priority,rowid LIMIT 25").fetchall():
            try:
                result = await self.worker.monday.call('query($ids:[ID!]!){items(ids:$ids){id board{id} updates(limit:100){id body}}}', {'ids':[row['item_id']]})
                items = result['items']
                if len(items) != 1 or str(items[0]['id']) != row['item_id'] or str(items[0]['board']['id']) != '424237712':
                    continue
                marker = 'ESCS-AUDIT-' + row['event_key'][:24]
                matches = [u for u in items[0].get('updates', []) if marker in (u.get('body') or '')]
                if matches:
                    with self.db:
                        self.db.execute("UPDATE pulse_audit_event SET state='posted',update_id=? WHERE event_key=?", (str(matches[0]['id']), row['event_key']))
                    continue
                if row['state'] == 'unknown':
                    continue  # No automatic duplicate after an uncertain outcome.
                with self.db:
                    reserved = self.db.execute("UPDATE pulse_audit_event SET state='sending' WHERE event_key=? AND state='pending'", (row['event_key'],)).rowcount
                if reserved != 1:
                    continue  # Another processing cycle owns or has completed this post.
                try:
                    response = await self.worker.monday.call('mutation($item:ID!,$body:String!){create_update(item_id:$item,body:$body){id}}', {'item':row['item_id'], 'body':row['body']})
                    update_id = response['create_update']['id']
                    if not str(update_id).isdigit():
                        raise ValueError('Pulse audit post unconfirmed')
                except Exception:
                    with self.db:
                        self.db.execute("UPDATE pulse_audit_event SET state='unknown' WHERE event_key=?", (row['event_key'],))
                else:
                    with self.db:
                        self.db.execute("UPDATE pulse_audit_event SET state='posted',update_id=? WHERE event_key=?", (str(update_id), row['event_key']))
            except Exception:
                pass  # Read failure leaves the pending event available for the next cycle.
        counts = dict(self.db.execute('SELECT state,count(*) FROM pulse_audit_event GROUP BY state').fetchall())
        self.worker.state['pulseAudit'] = {'enabled':True, 'posted':counts.get('posted',0), 'pending':counts.get('pending',0), 'uncertain':counts.get('unknown',0)}
        if counts != self.last_counts:
            import logging
            logging.getLogger('escs.automatic.followup').info('Recruitment pulse audit: %s', json.dumps(self.worker.state['pulseAudit']))
            self.last_counts = counts
