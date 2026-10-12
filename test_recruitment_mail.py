import json
import os
import unittest
from unittest.mock import patch

import httpx
from recruitment_mail import RecruitmentMail, JOBS_EMAIL, build_recruitment_mail_router, validate_window
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

BASE = "https://escs.bitrix24.com/rest/api/1/testcredential"
ROW = {"id": 12, "mailboxId": 107, "mailboxEmail": JOBS_EMAIL,
       "from": "Referee <referee@example.org>", "to": JOBS_EMAIL,
       "subject": "Reference", "body": "Reference text", "hasAttachments": True}


class MailTests(unittest.IsolatedAsyncioTestCase):
    def client(self, message=None, mailbox_email=JOBS_EMAIL, fail=False):
        self.calls = []
        def respond(request):
            self.calls.append((request.url.path.split("/")[-1], json.loads(request.content)))
            method = self.calls[-1][0]
            if fail:
                return httpx.Response(403, json={"error": "denied", "error_description": BASE})
            row = dict(ROW if message is None else message)
            if method == "mail.mailbox.get":
                result = {"item": {"id": 107, "email": mailbox_email}}
            elif method == "mail.message.list":
                result = {"items": [row]}
            else:
                fields = self.calls[-1][1]["select"]
                result = {"item": {k: v for k, v in row.items() if k in fields}}
            return httpx.Response(200, json={"result": result})
        return RecruitmentMail(BASE, 107, httpx.MockTransport(respond))

    async def test_metadata_search_does_not_return_body(self):
        result = await self.client().search("2026-10-01T00:00:00+01:00", "2026-10-12T00:00:00+01:00")
        self.assertNotIn("body", result["messages"][0])
        self.assertEqual(self.calls[1][1]["mailboxId"], 107)
        self.assertNotIn("isSeen", self.calls[1][1])

    async def test_get_checks_identity_before_requesting_body(self):
        result = await self.client().get(12)
        self.assertEqual(result["body"], "Reference text")
        self.assertNotIn("body", self.calls[1][1]["select"])
        self.assertIn("body", self.calls[2][1]["select"])

    async def test_other_mailbox_body_is_never_requested(self):
        c = self.client(message={**ROW, "mailboxId": 99, "mailboxEmail": "hr@securecareservices.co.uk"})
        with self.assertRaises(RuntimeError):
            await c.get(12)
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("body", self.calls[1][1]["select"])

    async def test_wrong_mailbox_configuration_blocks_search(self):
        with self.assertRaises(RuntimeError):
            await self.client(mailbox_email="hr@securecareservices.co.uk").search(
                "2026-10-01T00:00:00Z", "2026-10-12T00:00:00Z")
        self.assertEqual(len(self.calls), 1)

    async def test_outgoing_mail_is_excluded(self):
        c = self.client(message={**ROW, "from": JOBS_EMAIL, "to": "referee@example.org"})
        result = await c.search("2026-10-01T00:00:00Z", "2026-10-12T00:00:00Z")
        self.assertEqual(result["messages"], [])
        with self.assertRaises(RuntimeError):
            await c.get(12)

    async def test_provider_errors_do_not_expose_secret(self):
        with self.assertRaises(RuntimeError) as caught:
            await self.client(fail=True).get(12)
        self.assertNotIn("testcredential", str(caught.exception))

    async def test_body_is_bounded(self):
        result = await self.client(message={**ROW, "body": "x" * 25000}).get(12)
        self.assertEqual(len(result["body"]), 24000)
        self.assertTrue(result["bodyTruncated"])

    async def test_pagination_retains_provider_count_after_filtering(self):
        c = self.client(message={**ROW, "from": JOBS_EMAIL})
        result = await c.search("2026-10-01T00:00:00Z", "2026-10-12T00:00:00Z", limit=1)
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["nextPage"], 2)

    async def test_invalid_message_id_is_not_sent(self):
        c = self.client()
        for value in (0, -1, True):
            with self.assertRaises(ValueError):
                await c.get(value)
        self.assertEqual(self.calls, [])

    def test_invalid_host_or_account_is_rejected(self):
        for value in (BASE.replace("escs.bitrix24.com", "example.org"),
                      BASE.replace("/api/1/", "/api/2/"), BASE + "?secret=x",
                      BASE.replace("https:", "http:")):
            with self.assertRaises(ValueError):
                RecruitmentMail(value, 107)

    def test_copied_webhook_base_is_normalised_for_rest_v3(self):
        c = RecruitmentMail(BASE.replace("/rest/api/", "/rest/"), 107)
        self.assertEqual(c._url, BASE)

    def test_date_windows_require_timezone_and_bounds(self):
        for start, end in (("2026-10-01", "2026-10-12"),
                           ("2026-10-12T00:00:00Z", "2026-10-01T00:00:00Z"),
                           ("2026-01-01T00:00:00Z", "2026-10-12T00:00:00Z")):
            with self.assertRaises(ValueError):
                validate_window(start, end)

    def protected_app(self):
        def guard(x_connector_key: str | None = Header(default=None)):
            if x_connector_key != "test-key":
                raise HTTPException(401, "Unauthorized")
        app = FastAPI()
        app.include_router(build_recruitment_mail_router(guard))
        return TestClient(app)

    def test_all_mail_routes_require_authentication(self):
        with patch.dict(os.environ, {"ESCS_RECRUITMENT_MAIL_READ_ENABLED": "true"}, clear=True):
            client = self.protected_app()
            for path in ("/recruitment-mail/status", "/recruitment-mail/search",
                         "/recruitment-mail/messages/12"):
                self.assertEqual(client.get(path).status_code, 401)
                self.assertEqual(client.get(path, headers={"X-Connector-Key": "wrong"}).status_code, 401)

    def test_authenticated_disabled_reader_blocks_mail_reads(self):
        with patch.dict(os.environ, {}, clear=True):
            client = self.protected_app()
            headers = {"X-Connector-Key": "test-key"}
            status = client.get("/recruitment-mail/status", headers=headers)
            self.assertFalse(status.json()["enabled"])
            self.assertEqual(client.get("/recruitment-mail/messages/12", headers=headers).status_code, 503)

    def test_no_mcp_mail_tool_registration_is_available(self):
        import recruitment_mail
        self.assertFalse(hasattr(recruitment_mail, "register_recruitment_mail_tools"))


if __name__ == "__main__":
    unittest.main()
