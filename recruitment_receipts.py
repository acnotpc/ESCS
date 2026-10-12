"""Draft receipt checks for explicitly verified candidate-to-submission links.

No credential handling, medical interpretation, provider writes or sender.
Read callbacks must be scoped to the three approved forms before activation.
"""
from dataclasses import dataclass
import os
from datetime import date

EXTENDED_ENABLED = os.getenv('HR_FOLLOWUP_EXTENDED_FORMS', '').lower() == 'true'
# Manual routes collect receipt only; they never assert approval or clearance.
EXTENDED_REQUESTS = {
 6: ('contract_fulltime', 'Full-time support worker contract', '211685006073349', 'Signature'),
 7: ('contract_bank', 'Bank support worker contract', '220163719355354', 'Signature'),
 8: ('contract_control', 'Control room contract', '91714156281355', 'Signature'),
 9: ('contract_hr', 'HR administrator contract', '212234621855351', 'Signature'),
 10: ('dress_code', 'Dress code declaration', '211613940249352', 'Signed'),
 11: ('driving_declaration', 'Driving declaration', '223043594296359', 'Signature'),
 12: ('driving_consent', 'Driving entitlement consent', '213556102255044', 'Signature'),
 13: ('dbs_consent', 'DBS Update Service consent', '211753252627050', 'Signature'),
 14: ('licence_details', 'Licence check information', '73002266184349', None),
 15: ('payroll', 'Payroll information', '51518125846861', None),
 16: ('training', 'Training arrangements', None, None),
 17: ('taster', 'Taster shift arrangements', None, None),
 18: ('uniform', 'Uniform issue arrangements', None, None),
 19: ('id_badge', 'ID badge issue arrangements', None, None),
 20: ('emergency_contacts', 'Emergency contact details', '90715470243352', None),
 21: ('sia_evidence', 'SIA evidence', '92583319416361', None),
}

FORMS = {
    'history': ('211923425771051', 'Signature'),
    'medical': ('90715470243352', 'Signature'),
    'working_time': ('82551787411460', 'Signed'),
}
if EXTENDED_ENABLED:
    FORMS.update({kind:(form_id,signature) for kind,_,form_id,signature
                  in EXTENDED_REQUESTS.values() if signature})
CHOICE = 'OPT OUT/OPT IN/CANCEL OPT OUT'


class ReceiptHold(ValueError):
    pass


@dataclass(frozen=True)
class ReceiptLink:
    candidate_id: str
    kind: str
    submission_id: str
    verified: bool = False


def field(collection, title):
    if not isinstance(collection, dict):
        raise ReceiptHold('schema_or_answers_unavailable')
    matches = [v for v in collection.values() if isinstance(v, dict) and v.get('text') == title]
    if len(matches) != 1:
        raise ReceiptHold('required_field_missing_or_ambiguous')
    return matches[0]


def validate_receipt(link, schema, record):
    if (not isinstance(link, ReceiptLink) or link.verified is not True
            or link.kind not in FORMS or not isinstance(link.candidate_id, str)
            or not link.candidate_id.isdigit() or not isinstance(link.submission_id, str)
            or not link.submission_id.isdigit()):
        raise ReceiptHold('verified_link_required')
    form_id, signature_title = FORMS[link.kind]
    if (not isinstance(record, dict) or str(record.get('id')) != link.submission_id
            or str(record.get('form_id')) != form_id or record.get('status') != 'ACTIVE'):
        raise ReceiptHold('canonical_receipt_identity')
    signature_schema = field(schema, signature_title)
    signature = field(record.get('answers'), signature_title)
    if (signature_schema.get('type') != 'control_signature' or signature.get('type') != 'control_signature'
            or str(signature_schema.get('qid')) != str(signature.get('qid'))
            or not isinstance(signature.get('answer'), str) or not signature['answer'].strip()):
        raise ReceiptHold('signature_receipt_requires_review')
    if link.kind == 'working_time':
        choice_schema, choice = field(schema, CHOICE), field(record.get('answers'), CHOICE)
        options = choice_schema.get('options')
        if not isinstance(options, str) or not options:
            raise ReceiptHold('working_time_schema_requires_review')
        allowed = options.split('|')
        if (choice_schema.get('type') not in {'control_radio', 'control_dropdown'}
                or choice.get('type') != choice_schema.get('type')
                or str(choice_schema.get('qid')) != str(choice.get('qid'))
                or not isinstance(choice.get('answer'), str) or choice['answer'] not in allowed):
            raise ReceiptHold('working_time_choice_requires_review')
    if link.kind in {row[0] for row in EXTENDED_REQUESTS.values()}:
        validate_extended_fields(link.kind, schema, record)
    # Return only identifiers; never medical answers, signature URLs or choice content.
    return link.candidate_id, link.kind, link.submission_id


def validate_extended_fields(kind, schema, record):
    """Presence and selected-option checks only; no interpretation of declarations."""
    titles = ['Name', 'Date']
    if kind == 'contract_bank':
        titles[1] = 'Date it was signed. This is not the start date'
    if kind.startswith('contract_'):
        titles += ['E-mail', 'I accept the terms of this employment contract and acknowledge receipt of a copy of this Contract of Employment which is in accordance with the provisions of the Employment Rights Act 1996 as amended from time to time.']
    elif kind == 'dress_code':
        titles += ['Email', 'I have read and understood the policies']
    elif kind == 'driving_consent':
        titles += ['Driver Licence Number', 'Current Address', 'Date of Birth']
    elif kind == 'driving_declaration':
        titles += ['Have you been involved in any collisions/accidents in the last 5 (five) years, which involve engagement with an insurance company and/or the Police.',
                   'Have you been refused, declined or insurance cancelled in the last 5 (five) years or had any special terms imposed?',
                   'Does your licence have over 6 points or the following conviction codes:  AC, BA, CD, CU, DD, DG, DR, IN, TT, UT',
                   'Been a UK resident for more than 5 years', 'Have you held a UK Full Driving licence for longer than 12 months?']
    elif kind == 'dbs_consent':
        titles += ['Email']
    for title in titles:
        q, a = field(schema, title), field(record.get('answers'), title)
        value = a.get('answer')
        if (a.get('type') != q.get('type') or str(a.get('qid')) != str(q.get('qid'))
                or not present(value)):
            raise ReceiptHold('extended_form_incomplete_or_changed')
        if q.get('type') in {'control_checkbox', 'control_radio', 'control_dropdown'}:
            options = q.get('options')
            selected = value if isinstance(value, list) else [value]
            if not isinstance(options,str) or not selected or any(v not in options.split('|') for v in selected):
                raise ReceiptHold('extended_declaration_requires_review')
            if kind == 'dress_code' and title == 'I have read and understood the policies' and selected != ['Yes']:
                raise ReceiptHold('extended_declaration_requires_review')
            if kind.startswith('contract_') and title.startswith('I accept') and len(options.split('|')) != 1:
                raise ReceiptHold('contract_acceptance_schema_requires_review')
        if title in {'Date', 'Date of Birth', 'Date it was signed. This is not the start date'}:
            try:
                if not isinstance(value,dict): raise ValueError('date')
                date(int(value['year']), int(value['month']), int(value['day']))
            except (ValueError,TypeError,KeyError):
                raise ReceiptHold('extended_date_requires_review') from None


def present(value):
    if isinstance(value,str): return bool(value.strip())
    if isinstance(value,dict): return bool(value) and any(present(v) for v in value.values())
    if isinstance(value,list): return bool(value) and all(present(v) for v in value)
    return False


async def apply_receipts(ledger, links, read_schema, read_submission):
    """Validate the entire batch before applying receipts; failure blocks sending.

    Missing candidate links remain held; a failed lookup never means no response.
    Callback implementations are still required before live activation.
    """
    links = list(links)
    seen = {}
    for link in links:
        if not isinstance(link, ReceiptLink) or link.kind not in FORMS or not link.verified:
            raise ReceiptHold('verified_link_required')
        identity = (FORMS[link.kind][0], link.submission_id)
        previous = seen.get(identity)
        if previous and previous != (link.candidate_id, link.kind):
            raise ReceiptHold('receipt_linked_to_multiple_candidates')
        seen[identity] = (link.candidate_id, link.kind)
    actions, schemas = [], {}
    for link in links:
        form_id = FORMS[link.kind][0]
        if form_id not in schemas:
            schemas[form_id] = await read_schema(form_id)
        record = await read_submission(form_id, link.submission_id)
        actions.append(validate_receipt(link, schemas[form_id], record))
    received = 0
    for candidate, kind, evidence_id in actions:
        owner = ledger.db.execute('SELECT candidate_id FROM checklist_requirement WHERE kind=? AND evidence_id=?',
                                  (kind, evidence_id)).fetchone()
        if owner and owner['candidate_id'] != candidate:
            raise ReceiptHold('receipt_already_linked_to_another_candidate')
    for candidate, kind, evidence_id in actions:
        received += ledger.receive(candidate, kind, evidence_id, exact_match=True)
    return {'received': received}
