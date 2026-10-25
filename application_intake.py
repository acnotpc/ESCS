"""Administrative application intake; receipt never means clearance or acceptance."""
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from automatic_followup import values, text, data, names, normalized, PAUSE

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
        for a in answers.values():
            if not isinstance(a, dict):
                raise ValueError('Receipt schema changed')
            title = normalized(a.get('text', '')).rstrip(':')
            value = a.get('answer')
            if title in NAME_TITLES:
                keys.update(('name', self.auto.index.digest(n)) for n in names(value))
            elif title in EMAIL_TITLES and isinstance(value, str) and '@' in value:
                keys.add(('email', self.auto.index.digest(normalized(value))))
            elif title == 'date of birth' and dob(value):
                keys.add(('dob', self.auto.index.digest(dob(value))))
        if not any(t == 'name' for t, _ in keys):
            raise ValueError('Receipt name unavailable')
        return keys

    async def refresh(self, source, kind, form):
        old = self.db.execute('SELECT watermark FROM intake_source WHERE kind=?', (kind,)).fetchone()
        watermark = old['watermark'] if old else 0
        largest = watermark
        rows = []
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
                    rows.extend((kind, str(sid), typ, digest) for typ, digest in self.keys(row.get('answers')))
            if reached or len(page) < 100:
                break
        else:
            raise ValueError('Incomplete intake archive')
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO intake_archive VALUES(?,?,?,?)', rows)
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
        return None, bool(matches)

    async def write(self, item_id, writes):
        result = await self.worker.monday.call(
            'mutation($item:ID!,$value:JSON!){change_multiple_column_values(board_id:424237712,item_id:$item,column_values:$value){id}}',
            {'item':item_id, 'value':json.dumps(writes)})
        if str(result['change_multiple_column_values']['id']) != item_id:
            raise ValueError('Intake update unconfirmed')

    async def run(self, source, roster, counts, name_counts, now):
        from checklist_workflow import REQUEST, RECEIVED, SUMMARY
        phase = 'application_archive'
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
                    continue  # Fresh stage must be read next cycle before requesting/sending.
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
                else:
                    with self.db:
                        self.db.execute("UPDATE application_intake SET state='active',sends=sends+1,due=? WHERE id=?", ((now + timedelta(hours=72)).isoformat(), item_id))
                        self.db.execute("UPDATE intake_attempt SET state='accepted' WHERE id=?", (attempt,))
            self.state = {'enabled': True, 'lastSuccess':now.isoformat(), 'enrolled':enrolled, 'active':active, 'received':completed, 'held':held, 'error':None}
        except Exception:
            self.state = {'enabled':True, 'error':'application_intake_requires_review', 'failedPhase':phase}
        self.worker.state['applicationIntake'] = dict(self.state)
        import logging
        logging.getLogger('escs.automatic.followup').info('Application intake cycle: %s', json.dumps(self.state))
        return self.state
