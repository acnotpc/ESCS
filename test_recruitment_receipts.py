import unittest
from recruitment_receipts import ReceiptLink, ReceiptHold, validate_receipt, apply_receipts, FORMS, CHOICE


def sample(kind='medical', sid='456'):
    form, title = FORMS[kind]
    schema = {'1': {'qid': '1', 'text': title, 'type': 'control_signature'}}
    record = {'id': sid, 'form_id': form, 'status': 'ACTIVE', 'answers': {
        '1': {'qid': '1', 'text': title, 'type': 'control_signature', 'answer': 'signed'},
        '9': {'text': 'Medical Conditions', 'answer': 'private health content'}}}
    if kind == 'working_time':
        schema['2'] = {'qid': '2', 'text': CHOICE, 'type': 'control_radio', 'options': 'IN|OUT|CANCEL OPT OUT'}
        record['answers']['2'] = {'qid': '2', 'text': CHOICE, 'type': 'control_radio', 'answer': 'IN'}
    return schema, record


class ReceiptTests(unittest.TestCase):
    def test_identifiers_only(self):
        schema, record = sample()
        self.assertEqual(('123', 'medical', '456'), validate_receipt(ReceiptLink('123', 'medical', '456', True), schema, record))

    def test_verified_id_required(self):
        with self.assertRaises(ReceiptHold):
            validate_receipt(ReceiptLink('123', 'medical', '456'), *sample())

    def test_wrong_form_and_deleted_receipt_held(self):
        for key, value in [('form_id', '999'), ('id', '999'), ('status', 'DELETED')]:
            schema, record = sample()
            record[key] = value
            with self.assertRaises(ReceiptHold):
                validate_receipt(ReceiptLink('123', 'medical', '456', True), schema, record)

    def test_blank_or_duplicate_signature_held(self):
        for mode in ['blank', 'duplicate']:
            schema, record = sample()
            if mode == 'blank':
                record['answers']['1']['answer'] = ''
            else:
                record['answers']['2'] = dict(record['answers']['1'])
            with self.assertRaises(ReceiptHold):
                validate_receipt(ReceiptLink('123', 'medical', '456', True), schema, record)

    def test_either_choice_and_cancellation_stop(self):
        for choice in ['IN', 'OUT', 'CANCEL OPT OUT']:
            schema, record = sample('working_time')
            record['answers']['2']['answer'] = choice
            self.assertEqual(('123', 'working_time', '456'), validate_receipt(
                ReceiptLink('123', 'working_time', '456', True), schema, record))

    def test_unrecognised_choice_held(self):
        schema, record = sample('working_time')
        record['answers']['2']['answer'] = 'unknown'
        with self.assertRaises(ReceiptHold):
            validate_receipt(ReceiptLink('123', 'working_time', '456', True), schema, record)


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_conflicting_links_block_before_read(self):
        async def no_read(*args):
            raise AssertionError('must not read')
        with self.assertRaises(ReceiptHold):
            await apply_receipts(None, [ReceiptLink('123', 'medical', '456', True),
                ReceiptLink('789', 'medical', '456', True)], no_read, no_read)

    async def test_failed_batch_does_not_partially_apply(self):
        class Ledger:
            def receive(self, *args, **kwargs):
                raise AssertionError('must not apply partial batch')
        async def schema(form):
            return sample()[0]
        async def read(form, sid):
            if sid == '789':
                raise RuntimeError('provider unavailable')
            return sample()[1]
        with self.assertRaises(RuntimeError):
            await apply_receipts(Ledger(), [ReceiptLink('123', 'medical', '456', True),
                ReceiptLink('234', 'medical', '789', True)], schema, read)
