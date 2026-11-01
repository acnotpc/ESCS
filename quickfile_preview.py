"""Prepare an invoice for review. This module has no upstream write capability."""
import hashlib
import json
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]
Quantity = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=4)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Evidence(StrictModel):
    # Source locators are assertions supplied by the operator, not automated checks.
    booking_form: str = Field(min_length=1, max_length=500)
    monday_actual_work: str = Field(min_length=1, max_length=500)
    fms_hours: str = Field(min_length=1, max_length=500)
    rac_mileage: str = Field(min_length=1, max_length=500)
    rate_agreement: str = Field(min_length=1, max_length=500)


class InvoiceLine(StrictModel):
    item_name: str = Field(min_length=1, max_length=25)
    description: str = Field(min_length=1, max_length=5000)
    nominal_code: int = Field(ge=1, le=9999, strict=True)
    qty: Quantity
    unit_cost: Money
    vat_rate: Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)]


class InvoiceProposal(StrictModel):
    job_number: str = Field(pattern=r'^CONVEYANCE-JOB-[0-9]{5,10}$')
    client_id: int = Field(gt=0, strict=True)
    issue_date: date
    term_days: int = Field(ge=0, le=365, strict=True)
    currency: Literal['GBP'] = 'GBP'
    purchase_reference: str | None = Field(default=None, max_length=100)
    notes: str = Field(max_length=10000)
    payment_term_notes: str = Field(max_length=10000)
    item_lines: list[InvoiceLine] = Field(min_length=1, max_length=100)
    evidence: Evidence

    @field_validator('notes', 'payment_term_notes')
    @classmethod
    def reject_minimum_hours(cls, value):
        # A wording check only; rates and chargeable hours still require review.
        import re
        text = re.sub(r'\s+', ' ', value.lower())
        if re.search(r'minimum.{0,60}(?:8|eight)[ -]?hours?|(?:8|eight)[ -]?hours?.{0,60}minimum', text):
            raise ValueError('Remove the eight-hour minimum wording before review.')
        return value


def prepare_invoice(proposal: InvoiceProposal):
    penny = Decimal('0.01')
    net = Decimal('0')
    vat = Decimal('0')
    calculations = []
    for line in proposal.item_lines:
        line_net = (line.qty * line.unit_cost).quantize(penny, rounding=ROUND_HALF_UP)
        line_vat = (line_net * line.vat_rate / 100).quantize(penny, rounding=ROUND_HALF_UP)
        net += line_net
        vat += line_vat
        calculations.append({'net': str(line_net), 'vat': str(line_vat),
                             'gross': str(line_net + line_vat)})
    # Decimal strings preserve exact values for review. A future write adapter
    # must encode the documented numeric JSON fields without float drift.
    payload = proposal.model_dump(mode='json', exclude={'job_number', 'evidence'})
    payload.update(type='invoice', description=proposal.job_number)
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True,
        separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return {
        'mode': 'preview-only', 'writes_supported': False,
        'created_in_quickfile': False, 'job_number': proposal.job_number,
        'proposal_sha256': fingerprint, 'proposed_payload': payload,
        'line_calculations': calculations,
        'totals': {'net': str(net.quantize(penny)), 'vat': str(vat.quantize(penny)),
                   'gross': str((net + vat).quantize(penny)), 'currency': 'GBP'},
        'evidence_references': proposal.evidence.model_dump(),
        'evidence_verified': False, 'duplicate_check_performed': False,
        'attachments_verified': False,
        'review_required': ['Confirm customer identity and purchase order requirements',
            'Compare original booking form with actual work, FMS hours and RAC mileage',
            'Confirm agreed rates, VAT treatment, quantities and payment terms',
            'Search all relevant invoices and credit notes for duplicate job charges',
            'Verify the actual booking-form attachment in QuickFile',
            'Obtain a second-person review before sending'],
    }


def build_quickfile_preview_router(require_key):
    router = APIRouter(prefix='/quickfile/previews', dependencies=[Depends(require_key)])

    @router.post('/invoice')
    async def preview(proposal: InvoiceProposal, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return prepare_invoice(proposal)

    return router
