"""Administrative follow-up ledger; deliberately has no email/provider access.

The caller must supply a verified candidate link and staff-authorised requests.
Receipt stops reminders, without recording any suitability/approval decision.
"""
import sqlite3
import html
from datetime import date, datetime, timedelta, timezone
from recruitment_receipts import EXTENDED_REQUESTS

REQUIREMENTS = {
    'history': ('10-year employment history and explanations of gaps', 'https://form.jotform.com/211923425771051'),
    'medical': ('medical disclosure form', 'https://form.jotform.com/90715470243352'),
    'right_to_work': ('right-to-work evidence or an appointment to check your documents', None),
    'working_time': ('your 48-hour working-time choice', 'https://form.jotform.com/82551787411460'),
    'dbs': ('outstanding DBS application details and identity documents', 'https://form.jotform.com/92583319416361'),
}
DBS_DETAILS = frozenset({'name', 'date_of_birth', 'previous_names_declaration',
    'current_address', 'five_year_address_history', 'identity_documents',
    'application_authorisation'})
INTERVAL = timedelta(hours=72)
MAX_SENDS = 4  # Initial request plus three reminders, shared across the checklist.
REQUIREMENTS.update({kind:(title, 'https://form.jotform.com/'+form_id if form_id else None)
                     for kind,title,form_id,_ in EXTENDED_REQUESTS.values()})


def history_gaps(periods, today, years=10):
    """Coverage only: dated work, education and explained gaps all count equally.

    Parsing unknown dates or an ambiguous form widget must be held for staff.
    No employer contact, authenticity check or employment decision is performed.
    """
    if type(today) is not date or years not in (5, 10):
        raise ValueError('Known coverage window required')
    try:
        start = today.replace(year=today.year-years)
    except ValueError:  # Leap-day calendar anniversary.
        start = today.replace(year=today.year-years, day=28)
    spans = []
    for first, last in periods:
        if type(first) is not date or type(last) is not date or first > last or last > today:
            raise ValueError('Invalid or uncertain dates require staff review')
        if last >= start:
            spans.append((max(start, first), last))
    gaps, cursor = [], start
    for first, last in sorted(spans):
        if first > cursor:
            gaps.append((cursor, first-timedelta(days=1)))
        cursor = max(cursor, last+timedelta(days=1))
    if cursor <= today:
        gaps.append((cursor, today))
    return gaps


def missing_dbs_details(presence):
    """Input contains presence flags only; never store names, documents or DOB."""
    if not isinstance(presence, dict) or set(presence) - DBS_DETAILS or any(type(v) is not bool for v in presence.values()):
        raise ValueError('Known boolean completeness flags required')
    return sorted(k for k in DBS_DETAILS if presence.get(k) is not True)


def utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('Timezone-aware time required')
    return value.astimezone(timezone.utc)


class Checklist:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS checklist_candidate(
                id TEXT PRIMARY KEY,state TEXT NOT NULL,sends INTEGER NOT NULL,
                next_due TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS checklist_requirement(
                candidate_id TEXT NOT NULL,kind TEXT NOT NULL,state TEXT NOT NULL,
                evidence_id TEXT,PRIMARY KEY(candidate_id,kind));
            CREATE UNIQUE INDEX IF NOT EXISTS checklist_receipt_owner
                ON checklist_requirement(kind,evidence_id) WHERE evidence_id IS NOT NULL;
        ''')

    def request(self, candidate_id, kinds, now, *, verified_link=False, staff_requested=False):
        kinds = set(kinds)
        if (not verified_link or not staff_requested or not isinstance(candidate_id, str)
                or not candidate_id.isdigit() or not kinds or not kinds <= REQUIREMENTS.keys()):
            raise ValueError('Verified linkage and explicit administrative request required')
        when = utc(now).isoformat()
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO checklist_candidate VALUES(?,?,?,?)',
                            (candidate_id, 'active', 0, when))
            for kind in kinds:
                # An existing receipt, hold, or exhausted campaign never reopens here.
                self.db.execute('INSERT OR IGNORE INTO checklist_requirement VALUES(?,?,?,NULL)',
                                (candidate_id, kind, 'awaiting'))

    def receive(self, candidate_id, kind, evidence_id, *, exact_match=False):
        if kind not in REQUIREMENTS or not exact_match or not isinstance(evidence_id, str) or not evidence_id.isdigit():
            raise ValueError('Exact linked receipt required')
        with self.db:
            owner = self.db.execute('SELECT candidate_id FROM checklist_requirement WHERE kind=? AND evidence_id=?',
                                    (kind, evidence_id)).fetchone()
            if owner and owner['candidate_id'] != candidate_id:
                raise ValueError('Receipt already linked to another candidate')
            changed = self.db.execute('UPDATE checklist_requirement SET state=?,evidence_id=? '
                'WHERE candidate_id=? AND kind=? AND state=?',
                ('received_review', evidence_id, candidate_id, kind, 'awaiting')).rowcount
            if not self.db.execute('SELECT 1 FROM checklist_requirement WHERE candidate_id=? AND state=?',
                                   (candidate_id, 'awaiting')).fetchone():
                self.db.execute('UPDATE checklist_candidate SET state=? WHERE id=? AND state=?',
                                ('received_review', candidate_id, 'active'))
        return bool(changed)

    def hold(self, candidate_id):
        with self.db:
            self.db.execute('UPDATE checklist_candidate SET state=? WHERE id=?', ('staff_review', candidate_id))

    def plan(self, candidate_id, now):
        row = self.db.execute('SELECT * FROM checklist_candidate WHERE id=?', (candidate_id,)).fetchone()
        when = utc(now)
        if not row or row['state'] != 'active' or when < datetime.fromisoformat(row['next_due']):
            return []
        if row['sends'] >= MAX_SENDS:
            self.hold(candidate_id)
            return []
        return [r['kind'] for r in self.db.execute(
            'SELECT kind FROM checklist_requirement WHERE candidate_id=? AND state=? ORDER BY kind',
            (candidate_id, 'awaiting'))]

    def reserve(self, candidate_id, now):
        # Recheck receipts immediately before reserving a single combined message.
        with self.db:
            kinds = self.plan(candidate_id, now)
            if not kinds:
                return []
            changed = self.db.execute('UPDATE checklist_candidate SET state=? WHERE id=? AND state=?',
                                     ('send_pending', candidate_id, 'active')).rowcount
        return kinds if changed else []

    def delivery(self, candidate_id, now, *, confirmed=False):
        with self.db:
            if not confirmed:
                self.db.execute('UPDATE checklist_candidate SET state=? WHERE id=? AND state=?',
                                ('staff_review', candidate_id, 'send_pending'))
                return
            waiting = self.db.execute('SELECT 1 FROM checklist_requirement WHERE candidate_id=? AND state=?',
                                     (candidate_id, 'awaiting')).fetchone()
            self.db.execute('UPDATE checklist_candidate SET state=?,sends=sends+1,next_due=? '
                'WHERE id=? AND state=?', ('active' if waiting else 'received_review',
                (utc(now) + INTERVAL).isoformat(), candidate_id, 'send_pending'))


def message(kinds, stage='pre_employment'):
    kinds = set(kinds)
    if not kinds or not kinds <= REQUIREMENTS.keys():
        raise ValueError('Known outstanding requirements required')
    if stage not in {'pre_employment', 'employed'}:
        raise ValueError('Verified employment stage required')
    contact = ('HR Team at hr@securecareservices.co.uk' if stage == 'employed'
               else 'Recruitment Team at jobs@securecareservices.co.uk')
    purpose = 'staff' if stage == 'employed' else 'recruitment'
    lines = ['Please complete the outstanding ' + purpose + ' information below. '
             'If you need help, contact the ' + contact + '.',
             'If you believe this information has already been submitted, please reply to this email so we can check our spam/junk folders and any recording errors. Please tell us which form and roughly when you submitted it. Please do not email bank details, medical information or identity documents.']
    for kind in sorted(kinds):
        title, link = REQUIREMENTS[kind]
        lines.append(title + (': ' + link if link else '. Please contact Recruitment to arrange the appropriate secure evidence/check route.'))
    if 'medical' in kinds:
        lines.append('Please use the medical form; do not email medical details or diagnoses.')
    if 'working_time' in kinds:
        lines.append('You may remain within the 48-hour average limit or voluntarily opt out if eligible. '
                     'Either choice is accepted; you will not be disadvantaged for declining to opt out.')
    if 'dbs' in kinds or 'right_to_work' in kinds:
        lines.append('Use the agreed secure form or document-check appointment; do not email identity documents.')
    if 'payroll' in kinds:
        lines.append('Use the encrypted payroll form; do not email bank details. HR will confirm receipt securely.')
    if kinds & {'training','taster','uniform','id_badge','licence_details','emergency_contacts','sia_evidence'}:
        lines.append('Contact the team to confirm arrangements or evidence already supplied. Staff will record receipt after checking your record; this does not approve the checks.')
    return '\n\n'.join(lines)


def html_message(kinds, stage='pre_employment'):
    body = html.escape(message(kinds, stage))
    for _, link in REQUIREMENTS.values():
        if link:
            escaped = html.escape(link)
            body = body.replace(escaped, '<a href="' + escaped + '">Open secure form</a> ('
                                + '<a href="' + escaped + '">' + escaped + '</a>)')
    return '<p>' + body.replace('\n\n', '</p><p>') + '</p>'
