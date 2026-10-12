"""Approved read-only access extension; no enrollment, writes or email sending."""
import os
import re
import httpx
from fastapi import APIRouter, Depends, HTTPException

from recruitment_receipts import FORMS as RECEIPT_FORMS
FORMS = {kind:details[0] for kind,details in RECEIPT_FORMS.items()}
SIGNATURES = {kind:details[1] for kind,details in RECEIPT_FORMS.items()}
CHOICE = 'OPT OUT/OPT IN/CANCEL OPT OUT'
TRACKING = 'ESCS follow-up request ID'


class ChecklistRead:
    def __init__(self, key, transport=None):
        if not key:
            raise RuntimeError('Recruitment read connection is not configured')
        self._key, self.transport = key, transport

    async def _get(self, path):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False, transport=self.transport) as client:
                response = await client.get('https://eu-api.jotform.com' + path,
                                            headers={'APIKEY': self._key})
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise RuntimeError('Checklist source read failed') from None
        if not response.is_success or not isinstance(payload, dict) or payload.get('responseCode') != 200:
            raise RuntimeError('Checklist source read rejected')
        return payload.get('content')

    async def schema(self, form_id):
        if form_id not in FORMS.values():
            raise ValueError('Form is outside the approved checklist scope')
        questions = await self._get('/form/' + form_id + '/questions')
        if not isinstance(questions, dict) or any(not isinstance(q, dict) or not str(qid).isdigit()
                                                  for qid, q in questions.items()):
            raise RuntimeError('Checklist schema unavailable')
        return {qid: dict(q, qid=qid) for qid, q in questions.items()}

    async def submission(self, form_id, submission_id):
        if form_id not in FORMS.values() or not isinstance(submission_id, str) or not submission_id.isdigit():
            raise ValueError('Verified checklist submission ID required')
        record = await self._get('/submission/' + submission_id)
        if (not isinstance(record, dict) or str(record.get('id')) != submission_id
                or str(record.get('form_id')) != form_id or record.get('status') != 'ACTIVE'):
            raise RuntimeError('Checklist receipt identity mismatch')
        # Callback-only internal record. No route exposes applicant answers.
        if isinstance(record.get('answers'), dict):
            record = dict(record, answers={qid: dict(a, qid=qid) for qid, a in record['answers'].items()
                                          if isinstance(a, dict)})
        return record

    async def inspect_schemas(self):
        result = {}
        for kind, form_id in FORMS.items():
            schema = await self.schema(form_id)
            matches = [q for q in schema.values() if q.get('text') == SIGNATURES[kind]]
            valid = len(matches) == 1 and matches[0].get('type') == 'control_signature'
            item = {'formId': form_id, 'signatureSchemaValid': valid}
            tracking = [q for q in schema.values() if q.get('text') == TRACKING]
            item['trackingSchemaValid'] = (len(tracking) == 1
                and tracking[0].get('type') == 'control_textbox'
                and tracking[0].get('hidden') == 'Yes'
                and tracking[0].get('required') != 'Yes'
                and not tracking[0].get('defaultValue')
                and bool(re.fullmatch(r'[A-Za-z0-9_]+', str(tracking[0].get('name', '')))))
            if kind == 'working_time':
                choices = [q for q in schema.values() if q.get('text') == CHOICE]
                choice_valid = (len(choices) == 1 and choices[0].get('type') in {'control_radio', 'control_dropdown'}
                    and isinstance(choices[0].get('options'), str) and bool(choices[0]['options']))
                item['choiceSchemaValid'] = choice_valid
            result[kind] = item
        return result


def build_checklist_access_router(require_key, reader=None):
    router = APIRouter(prefix='/recruitment-checklist', dependencies=[Depends(require_key)])

    @router.get('/access')
    async def access():
        return {'approvedForms': FORMS, 'readOnly': True,
                'connectionConfigured': bool(reader or os.getenv('JOTFORM_RECRUITMENT_READ_API_KEY')),
                'candidateEmailsEnabled': False, 'automaticEnrollmentEnabled': False}

    @router.get('/schemas')
    async def schemas():
        try:
            source = reader or ChecklistRead(os.getenv('JOTFORM_RECRUITMENT_READ_API_KEY', ''))
            return {'forms': await source.inspect_schemas(), 'candidateEmailsEnabled': False}
        except (RuntimeError, ValueError):
            raise HTTPException(503, 'Checklist schema verification unavailable; candidate chasing remains disabled') from None

    return router
