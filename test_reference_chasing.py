import copy
import asyncio
import tempfile
import unittest
import httpx
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from datetime import datetime, timedelta, timezone

from reference_chasing import (Ledger, Engine, SOURCE_FORM, CONSENT, REFEREES,
                               canonical, email_evidence, JOBS, request_body)
from reference_chasing_adapters import JotformRead, form_link, REQUEST_FIELD, ingest_new_sources
from reference_chasing_service import ChasingService, build_chasing_router

NOW = datetime(2026, 10, 12, 10, tzinfo=timezone.utc)


def submission(consent="Yes", sid="101"):
    fields = [(CONSENT, consent), ("Name", {"first": "Test", "last": "Candidate"}),
              (REFEREES[0], "first@example.org"), (REFEREES[1], "second@example.org")]
    return {"id": sid, "form_id": SOURCE_FORM, "status": "ACTIVE",
            "answers": {str(i): {"text": label, "answer": value} for i, (label, value) in enumerate(fields)}}


class ChasingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + "/references.sqlite"
        self.ledger = Ledger(self.path, 100)
        self.record = submission()
        self.rows = self.ledger.enroll(self.record, NOW)
        self.sent = []

    def tearDown(self):
        self.ledger.db.close()
        self.temp.cleanup()

    def engine(self, receipts=None, write=True, fail=False):
        async def source(sid):
            return copy.deepcopy(self.record)
        async def sender(to, subject, body):
            self.sent.append((to, subject, body))
            if fail:
                raise TimeoutError("Unknown delivery")
        async def empty(ledger):
            return None
        return Engine(self.ledger, source, sender, lambda row: "https://form.jotform.com/51517384496868", receipts or empty, write)

    async def test_only_explicit_yes_enrolls(self):
        for value in ("No", "", None, "yes", True):
            self.assertEqual(self.ledger.enroll(submission(value, "102"), NOW), [])

    async def test_missing_consent_fails_closed(self):
        value = submission(sid="102")
        del value["answers"]["0"]
        with self.assertRaises(ValueError):
            self.ledger.enroll(value, NOW)

    async def test_historical_baseline_excludes_old_default_yes(self):
        self.assertEqual(self.ledger.enroll(submission("Yes", "100"), NOW), [])

    async def test_replay_keeps_exact_two_request_ids(self):
        repeated = self.ledger.enroll(self.record, NOW + timedelta(days=1))
        self.assertEqual({r["id"] for r in repeated}, {r["id"] for r in self.rows})
        self.assertEqual(len(self.ledger.rows()), 2)

    async def test_duplicate_referee_addresses_fail_closed(self):
        value = submission(sid="102")
        value["answers"]["3"]["answer"] = "first@example.org"
        with self.assertRaises(ValueError):
            self.ledger.enroll(value, NOW)

    async def test_changed_nomination_does_not_create_extra_request(self):
        value = submission()
        value["answers"]["2"]["answer"] = "changed@example.org"
        with self.assertRaises(ValueError):
            self.ledger.enroll(value, NOW)
        self.assertEqual(len(self.ledger.rows()), 2)

    async def test_dry_run_does_not_send_or_advance_due(self):
        before = self.ledger.rows()
        await self.engine(write=False).tick(NOW)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.ledger.rows(), before)

    async def test_three_day_cadence_and_three_reminder_cap(self):
        engine = self.engine()
        await engine.tick(NOW)
        await engine.tick(NOW + timedelta(days=2, hours=23))
        self.assertEqual(len(self.sent), 2)
        for days in (3, 6, 9, 12, 15):
            await engine.tick(NOW + timedelta(days=days))
        self.assertEqual(len(self.sent), 8)  # initial + 3 reminders, for each referee
        self.assertTrue(all(r["state"] == "exhausted" for r in self.ledger.rows()))

    async def test_one_referee_receipt_stops_only_that_referee(self):
        engine = self.engine()
        await engine.tick(NOW)
        first = next(r for r in self.ledger.rows() if r["slot"] == 0)
        self.ledger.stop(first["id"], "form", "submission-12")
        await engine.tick(NOW + timedelta(days=3))
        self.assertEqual([r[0] for r in self.sent].count("first@example.org"), 1)
        self.assertEqual([r[0] for r in self.sent].count("second@example.org"), 2)

    async def test_mail_body_has_clickable_action_and_escaped_candidate(self):
        from html.parser import HTMLParser
        class Links(HTMLParser):
            def __init__(self):
                super().__init__()
                self.links = []
            def handle_starttag(self, tag, attrs):
                if tag == 'a':
                    self.links.append(dict(attrs).get('href'))
        url = 'https://form.jotform.com/51517384496868?nameOf=A%26B&escsReference=test'
        body = request_body('<script>candidate</script>', url)
        parser = Links()
        parser.feed(body)
        self.assertEqual(parser.links.count(url), 2)
        self.assertIn('Complete the reference form', body)
        self.assertIn('&lt;script&gt;candidate&lt;/script&gt;', body)
        self.assertNotIn('<script>', body)

    async def test_email_receipt_processed_before_due_send(self):
        engine = self.engine()
        await engine.tick(NOW)
        async def receipts(ledger):
            for row in ledger.rows():
                ledger.stop(row["id"], "email", "mail-12")
        engine.receipts = receipts
        await engine.tick(NOW + timedelta(days=3))
        self.assertEqual(len(self.sent), 2)

    async def test_failed_receipt_scan_prevents_sending(self):
        async def failed(ledger):
            raise RuntimeError("Receipt scan incomplete")
        with self.assertRaises(RuntimeError):
            await self.engine(receipts=failed).tick(NOW)
        self.assertEqual(self.sent, [])

    async def test_consent_withdrawal_stops_future_sends(self):
        engine = self.engine()
        await engine.tick(NOW)
        self.record = submission("No")
        await engine.tick(NOW + timedelta(days=3))
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(r["state"] == "consent_stopped" for r in self.ledger.rows()))

    async def test_unknown_send_does_not_retry_automatically(self):
        engine = self.engine(fail=True)
        await engine.tick(NOW)
        await engine.tick(NOW + timedelta(days=3))
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(r["state"] == "send_unknown" for r in self.ledger.rows()))

    async def test_receipt_can_resolve_an_unknown_send_without_resending(self):
        await self.engine(fail=True).tick(NOW)
        row = self.ledger.rows()[0]
        message = {"mailboxEmail": JOBS, "from": row["referee"], "to": JOBS,
                   "subject": "Re: [ESCS-REF:" + row["id"] + "]", "date": (NOW + timedelta(hours=1)).isoformat(),
                   "body": "Attached completed reference.", "hasAttachments": True}
        self.assertEqual(email_evidence(message, row), "received")
        self.ledger.stop(row["id"], "email", "mail-12")
        self.assertEqual(next(r for r in self.ledger.rows() if r["id"] == row["id"])["state"], "received")

    async def test_duplicate_receipt_preserves_first_evidence(self):
        self.ledger.stop(self.rows[0]["id"], "form", "first")
        self.ledger.stop(self.rows[0]["id"], "email", "later")
        row = next(r for r in self.ledger.rows() if r["id"] == self.rows[0]["id"])
        self.assertEqual(row["evidence_id"], "first")

    async def test_restart_holds_interrupted_send(self):
        with self.ledger.db:
            self.ledger.db.execute("UPDATE requests SET state='sending' WHERE id=?", (self.rows[0]["id"],))
        self.ledger.db.close()
        self.ledger = Ledger(self.path, 100)
        self.assertEqual(next(r for r in self.ledger.rows() if r["id"] == self.rows[0]["id"])["state"], "send_unknown")

    async def test_baseline_cannot_change_on_restart(self):
        with self.assertRaises(ValueError):
            Ledger(self.path, 99)

    def message(self, body="Please find attached the completed reference.", **changes):
        request = {**self.rows[0], "last_sent": NOW.isoformat()}
        value = {"mailboxEmail": JOBS, "from": request["referee"], "to": JOBS,
                 "subject": "Re: ESCS reference request [ESCS-REF:" + request["id"] + "]",
                 "date": (NOW + timedelta(hours=1)).isoformat(), "body": body, "hasAttachments": True}
        value.update(changes)
        return email_evidence(value, request)

    def test_explicit_matching_attached_reference_is_received_not_verified(self):
        self.assertEqual(self.message(), "received")

    def test_acknowledgement_and_future_promise_require_review(self):
        self.assertEqual(self.message("Thank you, I will send the reference tomorrow."), "review")

    def test_out_of_office_is_ignored(self):
        self.assertEqual(self.message("I am out of office."), "ignore")

    def test_quoted_completed_reference_is_not_receipt(self):
        self.assertEqual(self.message("Thanks.<blockquote>Attached completed reference.</blockquote>"), "review")

    def test_gmail_quoted_attachment_claim_is_not_receipt(self):
        self.assertEqual(self.message('Thanks.<div class="gmail_quote">Attached completed reference.</div>'), "review")

    def test_negative_attached_reference_is_not_receipt(self):
        self.assertEqual(self.message("The reference is not attached."), "review")

    def test_wrong_sender_or_missing_id_cannot_stop_request(self):
        self.assertEqual(self.message(**{"from": "wrong@example.org"}), "ignore")
        self.assertEqual(self.message(subject="Reference received"), "ignore")

    def test_tracking_link_is_specific_to_referee(self):
        schema = {REQUEST_FIELD: {"name": "requestId"}, "Name of candidate": {"name": "candidate"},
                  "Email address": {"name": "referee"}}
        first, second = (form_link(schema, r) for r in self.rows)
        self.assertNotEqual(first, second)
        self.assertIn(self.rows[0]["id"], first)
        self.assertNotIn(self.rows[1]["id"], first)

    async def test_jotform_key_is_in_header_not_url(self):
        calls = []
        def response(request):
            calls.append(request)
            return httpx.Response(200, json={"responseCode": 200, "content": submission()})
        client = JotformRead("test-read-key", httpx.MockTransport(response))
        await client.source("101")
        self.assertEqual(calls[0].headers["APIKEY"], "test-read-key")
        self.assertNotIn("test-read-key", str(calls[0].url))

    async def test_jotform_source_from_other_form_is_rejected(self):
        def response(request):
            return httpx.Response(200, json={"responseCode": 200,
                "content": {**submission(), "form_id": "123"}})
        with self.assertRaises(ValueError):
            await JotformRead("test-read-key", httpx.MockTransport(response)).source("101")

    async def test_jotform_pagination_collects_only_post_baseline(self):
        def response(request):
            return httpx.Response(200, json={"responseCode": 200,
                "content": [submission(sid="103"), submission(sid="102"), submission(sid="100")]})
        rows = await JotformRead("test-read-key", httpx.MockTransport(response)).submissions_after(SOURCE_FORM, 100)
        self.assertEqual([r["id"] for r in rows], ["102", "103"])

    async def test_tracking_schema_missing_field_blocks_link_creation(self):
        def response(request):
            return httpx.Response(200, json={"responseCode": 200, "content": {}})
        with self.assertRaises(RuntimeError):
            await JotformRead("test-read-key", httpx.MockTransport(response)).reference_schema()

    async def test_service_disabled_by_default_does_not_connect(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(await ChasingService().run_once(), {"enabled": False, "sent": 0})

    async def test_live_sending_blocked_while_native_workflow_is_active(self):
        env = {"ESCS_REFERENCE_CHASING_ENABLED": "true", "ESCS_REFERENCE_CHASING_WRITE_ENABLED": "true",
               "ESCS_REFERENCE_CHASING_DB": "/var/data/references.sqlite", "JOTFORM_RECRUITMENT_READ_API_KEY": "fake",
               "BITRIX_RECRUITMENT_MAIL_REST_URL": "https://escs.bitrix24.com/rest/1/fake",
               "BITRIX_RECRUITMENT_MAILBOX_ID": "107"}
        with patch.dict("os.environ", env, clear=True), patch("os.path.isdir", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "Native reference workflow"):
                await ChasingService().run_once()

    def test_every_operator_route_requires_authentication(self):
        def guard(x_connector_key: str | None = Header(default=None)):
            if x_connector_key != "test-key":
                raise HTTPException(401, "Unauthorized")
        app = FastAPI()
        app.include_router(build_chasing_router(ChasingService(), guard))
        client = TestClient(app)
        for method, path in (("GET", "/reference-chasing/status"), ("GET", "/reference-chasing/requests"),
                             ("GET", "/reference-chasing/review"),
                             ("POST", "/reference-chasing/run"),
                             ("POST", "/reference-chasing/requests/abc/confirm-email/12")):
            self.assertEqual(client.request(method, path).status_code, 401)


class ReviewAutomationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + '/review.sqlite'
        self.ledger = Ledger(self.path, 100)

    def tearDown(self):
        self.ledger.db.close()
        self.temp.cleanup()

    def source_adapter(self, records, failure=None):
        async def after(*args):
            return list(records.values())
        async def schema():
            return {}
        async def source(sid):
            if failure:
                raise failure
            return copy.deepcopy(records[sid])
        return SimpleNamespace(submissions_after=after, reference_schema=schema, source=source)

    async def test_bad_source_is_queued_once_without_blocking_other_applicant(self):
        bad = submission(sid='101')
        bad['answers']['2']['answer'] = 'invalid-email'
        records = {'101': bad, '102': submission(sid='102')}
        source = self.source_adapter(records)
        await ingest_new_sources(self.ledger, source)
        await ingest_new_sources(self.ledger, source)
        self.assertEqual([r['source_id'] for r in self.ledger.source_reviews()], ['101'])
        self.assertEqual(len(self.ledger.rows()), 2)
        self.assertTrue(all(r['source_id'] == '102' for r in self.ledger.rows()))
        self.assertNotIn('invalid-email', str(self.ledger.source_reviews()))
        records['101'] = submission(sid='101')
        await ingest_new_sources(self.ledger, source)
        self.assertEqual(self.ledger.source_reviews(), [])
        self.assertEqual(len(self.ledger.rows()), 4)

    async def test_provider_failure_is_not_treated_as_bad_applicant(self):
        source = self.source_adapter({'101': submission()}, RuntimeError('provider failed'))
        with self.assertRaises(RuntimeError):
            await ingest_new_sources(self.ledger, source)
        self.assertEqual(self.ledger.source_reviews(), [])
        self.assertEqual(self.ledger.rows(), [])

    async def test_field_drift_holds_both_nominees_and_sends_only_other_source(self):
        records = {'101': submission(), '102': submission(sid='102')}
        for record in records.values():
            self.ledger.enroll(record, NOW)
        del records['101']['answers']['1']
        calls, sent = [], []
        async def source(sid):
            calls.append(sid)
            return records[sid]
        async def sender(*args):
            sent.append(args)
        async def receipts(ledger):
            pass
        engine = Engine(self.ledger, source, sender, lambda r: 'https://form.jotform.com/51517384496868', receipts, True)
        await engine.tick(NOW)
        self.assertEqual(calls.count('101'), 1)
        self.assertEqual(len(sent), 2)
        self.assertTrue(all(r['state'] == 'source_review' for r in self.ledger.rows() if r['source_id'] == '101'))

    async def test_correction_resumes_original_nominee_without_reopening_received(self):
        rows = self.ledger.enroll(submission(), NOW)
        self.ledger.stop(rows[0]['id'], 'form', 'receipt-1')
        self.ledger.review_source('101', NOW)
        await ingest_new_sources(self.ledger, self.source_adapter({'101': submission()}))
        states = {r['id']: r['state'] for r in self.ledger.rows()}
        self.assertEqual(states[rows[0]['id']], 'received')
        self.assertEqual(states[rows[1]['id']], 'pending')
        self.assertEqual(self.ledger.source_reviews(), [])

    async def test_withdrawn_consent_closes_source_review_and_stops_requests(self):
        self.ledger.enroll(submission(), NOW)
        self.ledger.review_source('101', NOW)
        await ingest_new_sources(self.ledger, self.source_adapter({'101': submission('No')}))
        self.assertEqual(self.ledger.source_reviews(), [])
        self.assertTrue(all(r['state'] == 'consent_stopped' for r in self.ledger.rows()))

    async def test_review_queue_and_health_persist_after_restart(self):
        self.ledger.review_source('101', NOW)
        self.ledger.save_health({'lastSuccessAt': NOW.isoformat(), 'lastError': None})
        self.ledger.db.close()
        self.ledger = Ledger(self.path, 100)
        self.assertEqual(len(self.ledger.source_reviews()), 1)
        self.assertEqual(self.ledger.health()['lastSuccessAt'], NOW.isoformat())

    async def test_run_failure_records_safe_health_and_keeps_last_success(self):
        service = ChasingService()
        service.engine = SimpleNamespace(ledger=self.ledger)
        with patch.object(service, '_run_once', AsyncMock(return_value={'enabled': True, 'sent': 0})):
            await service.run_once()
        success = service.last_success_at
        with patch.object(service, '_run_once', AsyncMock(side_effect=RuntimeError('SECRET provider payload'))):
            with self.assertRaises(RuntimeError):
                await service.run_once()
        health = self.ledger.health()
        self.assertEqual(health['lastSuccessAt'], success)
        self.assertTrue(health['lastRunAt'])
        self.assertIsNotNone(health['lastError'])
        self.assertNotIn('SECRET', str(health))

    async def test_manual_and_background_runs_are_serialised(self):
        service = ChasingService()
        active, maximum = 0, 0
        async def run():
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1
            return {'enabled': True, 'sent': 0}
        with patch.object(service, '_run_once', run):
            await asyncio.gather(service.run_once(), service.run_once())
        self.assertEqual(maximum, 1)

    async def test_restart_restores_last_success_when_new_scan_fails(self):
        self.ledger.save_health({'lastSuccessAt': NOW.isoformat(), 'lastResult': {'enabled': True, 'sent': 0}})
        env = {'ESCS_REFERENCE_CHASING_ENABLED': 'true', 'ESCS_REFERENCE_CHASING_DB': '/var/data/test.sqlite',
               'ESCS_REFERENCE_SOURCE_BASELINE': '100', 'ESCS_REFERENCE_FORM_BASELINE': '200',
               'JOTFORM_RECRUITMENT_READ_API_KEY': 'fake', 'BITRIX_RECRUITMENT_MAILBOX_ID': '107',
               'BITRIX_RECRUITMENT_MAIL_REST_URL': 'https://escs.bitrix24.com/rest/1/fake'}
        service = ChasingService()
        with patch.dict('os.environ', env, clear=True), patch('os.path.isdir', return_value=True), \
             patch('reference_chasing_service.Ledger', return_value=self.ledger), \
             patch('reference_chasing_service.ingest_new_sources', AsyncMock(side_effect=RuntimeError('read failed'))):
            with self.assertRaises(RuntimeError):
                await service.run_once()
        self.assertEqual(service.last_success_at, NOW.isoformat())
        self.assertEqual(self.ledger.health()['lastSuccessAt'], NOW.isoformat())
        self.assertIsNotNone(service.last_error)

    async def test_protected_review_queue_exposes_only_requested_staff_records(self):
        self.ledger.enroll(submission(), NOW)
        self.ledger.review_source('101', NOW)
        service = ChasingService()
        service.engine = SimpleNamespace(ledger=self.ledger)
        def guard(x_connector_key: str | None = Header(default=None)):
            if x_connector_key != 'test-key':
                raise HTTPException(401, 'Unauthorized')
        app = FastAPI()
        app.include_router(build_chasing_router(service, guard))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            self.assertEqual((await client.get('/reference-chasing/review')).status_code, 401)
            result = (await client.get('/reference-chasing/review', headers={'X-Connector-Key': 'test-key'})).json()
            self.assertEqual(len(result['sourceReviews']), 1)
            self.assertEqual(len(result['referenceReviews']), 2)
            status = (await client.get('/reference-chasing/status', headers={'X-Connector-Key': 'test-key'})).json()
        self.assertEqual(status['reviewCounts']['sourceDetails'], 1)
        self.assertNotIn('first@example.org', str(status))


if __name__ == "__main__":
    unittest.main()
