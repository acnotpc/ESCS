"""Draft receipt checks for explicitly verified candidate-to-submission links.

No credential handling, medical interpretation, provider writes or sender.
Read callbacks must be scoped to the three approved forms before activation.
"""
from dataclasses import dataclass

FORMS = {
    'history': ('211923425771051', 'Signature'),
    'medical': ('90715470243352', 'Signature'),
    'working_time': ('82551787411460', 'Signed'),
}
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
    # Return only identifiers; never medical answers, signature URLs or choice content.
    return link.candidate_id, link.kind, link.submission_id


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
