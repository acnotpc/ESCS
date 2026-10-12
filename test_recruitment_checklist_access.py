import unittest
import httpx
from fastapi import FastAPI, Header, HTTPException
from recruitment_checklist_access import ChecklistRead, FORMS, SIGNATURES, CHOICE, build_checklist_access_router


class AccessTests(unittest.IsolatedAsyncioTestCase):
    def make_reader(self, handler):
        return ChecklistRead('test-only-key', httpx.MockTransport(handler))

    async def test_read_only_pinned_schema(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual('GET', request.method)
            self.assertEqual('eu-api.jotform.com', request.url.host)
            self.assertNotIn('test-only-key', str(request.url))
            return httpx.Response(200, json={'responseCode': 200, 'content': {
                '4': {'text': 'Signature', 'type': 'control_signature'}}})
        reader = self.make_reader(handler)
        schema = await reader.schema(FORMS['medical'])
        self.assertEqual('4', schema['4']['qid'])
        with self.assertRaises(ValueError):
            await reader.schema('999')
        self.assertEqual(1, len(calls))

    async def test_wrong_form_submission_held(self):
        reader = self.make_reader(lambda r: httpx.Response(200, json={'responseCode': 200,
            'content': {'id': '123', 'form_id': '999', 'status': 'ACTIVE'}}))
        with self.assertRaises(RuntimeError):
            await reader.submission(FORMS['medical'], '123')

    async def test_receipt_keys_normalised(self):
        reader = self.make_reader(lambda r: httpx.Response(200, json={'responseCode': 200,
            'content': {'id': '123', 'form_id': FORMS['history'], 'status': 'ACTIVE',
                        'answers': {'4': {'text': 'Signature', 'type': 'control_signature', 'answer': 'signed'}}}}))
        record = await reader.submission(FORMS['history'], '123')
        self.assertEqual('4', record['answers']['4']['qid'])

    async def test_provider_errors_do_not_leak(self):
        reader = self.make_reader(lambda r: httpx.Response(403, json={'responseCode': 403,
            'message': 'private test-only-key content'}))
        with self.assertRaises(RuntimeError) as caught:
            await reader.schema(FORMS['medical'])
        self.assertNotIn('test-only-key', str(caught.exception))

    async def test_schema_summary_contains_no_question_bodies(self):
        def handler(request):
            form_id = request.url.path.split('/')[2]
            kind = next(k for k, v in FORMS.items() if v == form_id)
            data = {'1': {'text': SIGNATURES[kind], 'type': 'control_signature'},
                    '9': {'text': 'private medical question', 'type': 'control_textbox'}}
            if kind == 'working_time':
                data['2'] = {'text': CHOICE, 'type': 'control_radio', 'options': 'IN|OUT'}
            return httpx.Response(200, json={'responseCode': 200, 'content': data})
        result = await self.make_reader(handler).inspect_schemas()
        self.assertTrue(result['medical']['signatureSchemaValid'])
        self.assertTrue(result['working_time']['choiceSchemaValid'])
        self.assertNotIn('private medical', str(result))

    async def test_protected_routes_and_no_submission_endpoint(self):
        async def guard(x_connector_key: str | None = Header(default=None)):
            if x_connector_key != 'test-key':
                raise HTTPException(401)
        class Reader:
            async def inspect_schemas(self):
                return {'medical': {'signatureSchemaValid': True}}
        app = FastAPI()
        app.include_router(build_checklist_access_router(guard, Reader()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            self.assertEqual(401, (await client.get('/recruitment-checklist/access')).status_code)
            result = await client.get('/recruitment-checklist/access', headers={'X-Connector-Key': 'test-key'})
            self.assertFalse(result.json()['candidateEmailsEnabled'])
            self.assertEqual(404, (await client.get('/recruitment-checklist/submissions/123',
                            headers={'X-Connector-Key': 'test-key'})).status_code)
