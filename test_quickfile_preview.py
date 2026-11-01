import unittest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from quickfile_preview import InvoiceProposal, prepare_invoice, build_quickfile_preview_router


def proposal():
    return dict(job_number='CONVEYANCE-JOB-99999', client_id=123,
        issue_date='2026-11-01', term_days=30, notes='', payment_term_notes='',
        item_lines=[dict(item_name='Mileage', description='Synthetic mileage',
            nominal_code=4001, qty='10.125', unit_cost='2.00', vat_rate='0')],
        evidence={k:'synthetic-reference' for k in ['booking_form', 'monday_actual_work',
            'fms_hours', 'rac_mileage', 'rate_agreement']})


class PreviewTests(unittest.TestCase):
    def test_exact_totals_and_no_false_verification(self):
        p = proposal()
        p['item_lines'][0].update(qty='0.3333', unit_cost='1.00', vat_rate='20')
        result = prepare_invoice(InvoiceProposal(**p))
        self.assertEqual(result['totals']['gross'], '0.40')
        self.assertEqual(result['proposed_payload']['description'], p['job_number'])
        self.assertFalse(result['created_in_quickfile'])
        self.assertFalse(result['evidence_verified'])
        self.assertFalse(result['duplicate_check_performed'])

    def test_hash_binds_proposed_content(self):
        p = proposal()
        first = prepare_invoice(InvoiceProposal(**p))['proposal_sha256']
        p['notes'] = 'Changed notes'
        self.assertNotEqual(first, prepare_invoice(InvoiceProposal(**p))['proposal_sha256'])

    def test_rejects_unsafe_or_incomplete_proposals(self):
        for key, value in [('client_id', True), ('currency', 'USD'),
            ('item_lines', []), ('evidence', {}), ('job_number', '123'),
            ('notes', 'Our minimum hours charged are 8 hours per person'),
            ('payment_term_notes', 'An eight-hour minimum applies')]:
            with self.subTest(key=key), self.assertRaises(ValidationError):
                InvoiceProposal(**{**proposal(), key:value})
        for amount in ['NaN', 'Infinity', '-1', '0.001']:
            p = proposal(); p['item_lines'][0]['unit_cost'] = amount
            with self.assertRaises(ValidationError): InvoiceProposal(**p)
        with self.assertRaises(ValidationError):
            InvoiceProposal(**{**proposal(), 'send_by_email':True})

    def test_protected_preview_and_no_write_routes(self):
        def guard(x_connector_key: str | None = Header(None)):
            if x_connector_key != 'synthetic-key': raise HTTPException(401)
        app = FastAPI(); app.include_router(build_quickfile_preview_router(guard))
        c = TestClient(app)
        self.assertEqual(c.post('/quickfile/previews/invoice', json=proposal()).status_code, 401)
        response = c.post('/quickfile/previews/invoice', json=proposal(),
            headers={'X-Connector-Key':'synthetic-key'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertFalse(response.json()['writes_supported'])
        for path in ['/quickfile/invoices', '/quickfile/invoices/1/send', '/quickfile/payments']:
            self.assertEqual(c.post(path, json={}).status_code, 404)


if __name__ == '__main__': unittest.main()
