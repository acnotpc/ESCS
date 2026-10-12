"""Draft exact request-to-form linkage; no sender or Monday access."""
import re
import secrets
from urllib.parse import urlencode
from recruitment_receipts import FORMS, ReceiptLink, ReceiptHold, field, validate_receipt

TRACKING = 'ESCS follow-up request ID'


def tracking_field(schema):
    q = field(schema, TRACKING)
    if (q.get('type') != 'control_textbox' or q.get('hidden') != 'Yes'
            or q.get('required') == 'Yes' or q.get('defaultValue')
            or not re.fullmatch(r'[A-Za-z0-9_]+', str(q.get('name', '')))):
        raise ReceiptHold('tracking_schema_requires_review')
    return q


class TrackingLinks:
    def __init__(self, ledger):
        self.ledger = ledger
        ledger.db.execute('''CREATE TABLE IF NOT EXISTS checklist_links(
            token TEXT PRIMARY KEY,candidate_id TEXT NOT NULL,kind TEXT NOT NULL,
            UNIQUE(candidate_id,kind))''')
        ledger.db.execute('''CREATE TABLE IF NOT EXISTS checklist_canonical_receipt_owner(
            form_id TEXT NOT NULL,submission_id TEXT NOT NULL,candidate_id TEXT NOT NULL,
            PRIMARY KEY(form_id,submission_id))''')
        ledger.db.commit()

    def issue(self, candidate_id, kind, schema):
        if kind not in FORMS:
            raise ReceiptHold('unsupported_tracking_form')
        q = tracking_field(schema)
        row = self.ledger.db.execute('SELECT r.state FROM checklist_requirement r '
            'JOIN checklist_candidate c ON c.id=r.candidate_id '
            'WHERE r.candidate_id=? AND r.kind=? AND c.state=?',
            (candidate_id, kind, 'active')).fetchone()
        if not row or row['state'] != 'awaiting':
            raise ReceiptHold('active_requested_item_required')
        with self.ledger.db:
            self.ledger.db.execute('INSERT OR IGNORE INTO checklist_links VALUES(?,?,?)',
                                  (secrets.token_hex(16), candidate_id, kind))
        token = self.ledger.db.execute('SELECT token FROM checklist_links WHERE candidate_id=? AND kind=?',
                                      (candidate_id, kind)).fetchone()['token']
        # Only an opaque random request ID goes in the URL, never staff ID or HR data.
        return 'https://form.jotform.com/' + FORMS[kind][0] + '?' + urlencode({q['name']: token})

    def accept(self, schema, record):
        q = tracking_field(schema)
        a = field(record.get('answers') if isinstance(record, dict) else None, TRACKING)
        token = a.get('answer')
        if (a.get('type') != 'control_textbox' or str(a.get('qid')) != str(q.get('qid'))
                or not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{32}', token)):
            raise ReceiptHold('canonical_request_id_required')
        link = self.ledger.db.execute('SELECT candidate_id,kind FROM checklist_links WHERE token=?',
                                      (token,)).fetchone()
        if not link or str(record.get('form_id')) != FORMS[link['kind']][0]:
            raise ReceiptHold('unknown_or_wrong_form_request')
        candidate, kind, evidence = validate_receipt(ReceiptLink(link['candidate_id'], link['kind'],
            str(record.get('id')), True), schema, record)
        form_id=FORMS[kind][0]
        owner=self.ledger.db.execute('SELECT candidate_id FROM checklist_canonical_receipt_owner WHERE form_id=? AND submission_id=?',
                                    (form_id,evidence)).fetchone()
        prior=self.ledger.db.execute('SELECT candidate_id FROM checklist_requirement WHERE kind=? AND evidence_id=?',
                                    (kind,evidence)).fetchone()
        if any(row and row['candidate_id']!=candidate for row in (owner,prior)):
            raise ReceiptHold('receipt_already_linked_to_another_candidate')
        received=self.ledger.receive(candidate, kind, evidence, exact_match=True)
        with self.ledger.db:
            self.ledger.db.execute('INSERT OR IGNORE INTO checklist_canonical_receipt_owner VALUES(?,?,?)',
                                   (form_id,evidence,candidate))
        return received
