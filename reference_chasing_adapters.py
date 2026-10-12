"""Draft canonical-source/receipt adapters. No activation or background task here."""
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from reference_chasing import (SOURCE_FORM, REFERENCE_FORM, JOBS, canonical,
                               answer, email, email_evidence, stamp)

REQUEST_FIELD = "ESCS reference request ID"


class JotformRead:
    def __init__(self, api_key, transport=None):
        if not api_key:
            raise ValueError("Dedicated read-only Jotform connection required")
        self._key, self.transport = api_key, transport

    async def _get(self, path, params=None):
        if not re.fullmatch(r"/(?:submission/\d+|form/(?:92583319416361|51517384496868)/(?:submissions|questions))", path):
            raise ValueError("Unsupported recruitment source read")
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False,
                                         transport=self.transport) as client:
                r = await client.get("https://eu-api.jotform.com" + path,
                                     params=params, headers={"APIKEY": self._key})
            payload = r.json()
        except (httpx.HTTPError, ValueError):
            raise RuntimeError("Recruitment source read failed") from None
        if not r.is_success or not isinstance(payload, dict) or payload.get("responseCode") != 200:
            raise RuntimeError("Recruitment source read rejected")
        return payload.get("content")

    async def source(self, submission_id):
        if not str(submission_id).isdigit():
            raise ValueError("Invalid source submission ID")
        record = await self._get("/submission/" + str(submission_id))
        if not isinstance(record, dict) or str(record.get("id")) != str(submission_id):
            raise RuntimeError("Source identity mismatch")
        canonical(record)  # validates pinned source form, consent schema and nominees
        return record

    async def submissions_after(self, form_id, baseline):
        if form_id not in {SOURCE_FORM, REFERENCE_FORM} or type(baseline) is not int or baseline < 0:
            raise ValueError("Invalid pinned form or baseline")
        found = {}
        previous = None
        for offset in range(0, 10000, 100):
            rows = await self._get("/form/" + form_id + "/submissions",
                                   {"orderby": "id", "limit": 100, "offset": offset})
            if not isinstance(rows, list):
                raise RuntimeError("Invalid submission page")
            reached = False
            for row in rows:
                if not isinstance(row, dict) or str(row.get("form_id")) != form_id or not str(row.get("id", "")).isdigit():
                    raise RuntimeError("Invalid canonical submission identity")
                sid = int(row["id"])
                # Validate descending order; never silently skip a differently sorted page.
                if previous is not None and sid > previous and sid not in found:
                    raise RuntimeError("Submission paging changed; retry complete scan")
                previous = sid
                if sid <= baseline:
                    reached = True
                else:
                    found[sid] = row
            if reached or len(rows) < 100:
                return [found[s] for s in sorted(found)]
        raise RuntimeError("Submission scan incomplete; no cursor advanced")

    async def reference_schema(self):
        questions = await self._get("/form/" + REFERENCE_FORM + "/questions")
        if not isinstance(questions, dict):
            raise RuntimeError("Reference form schema unavailable")
        fields = {}
        for label in (REQUEST_FIELD, "Name of candidate", "Email address"):
            matches = [q for q in questions.values() if isinstance(q, dict) and q.get("text") == label]
            if len(matches) != 1 or not re.fullmatch(r"[A-Za-z0-9_]+", str(matches[0].get("name", ""))):
                raise RuntimeError("Required reference tracking field is missing or ambiguous")
            fields[label] = matches[0]
        if fields[REQUEST_FIELD].get("type") != "control_textbox":
            raise RuntimeError("Request ID requires a short-text reference field")
        if fields["Name of candidate"].get("type") != "control_textbox":
            raise RuntimeError("Candidate prefill type needs review before activation")
        if fields["Email address"].get("type") != "control_email":
            raise RuntimeError("Referee email field needs review before activation")
        return fields


def form_link(schema, request):
    fields = {schema[REQUEST_FIELD]["name"]: request["id"],
              schema["Name of candidate"]["name"]: request["candidate"],
              schema["Email address"]["name"]: request["referee"]}
    return "https://form.jotform.com/" + REFERENCE_FORM + "?" + urlencode(fields)


class BitrixSender:
    """Internal adapter only. No public or conversational send API."""
    def __init__(self, mail, enabled=False):
        self.mail, self.enabled = mail, enabled

    async def __call__(self, recipient, subject, body):
        if not self.enabled:
            raise RuntimeError("Draft sender is disabled")
        recipient = email(recipient)
        if recipient == JOBS or not re.fullmatch(r"ESCS reference request(?: reminder)? \[ESCS-REF:[a-f0-9]{32}\]", subject):
            raise ValueError("Invalid reference sending destination or subject")
        await self.mail.verify_mailbox()
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False,
                                         transport=self.mail.transport) as client:
                r = await client.post(self.mail._url + "/mail.message.send", json={
                    "from": JOBS, "to": [recipient], "subject": subject, "body": body})
            payload = r.json()
        except (httpx.HTTPError, ValueError):
            raise RuntimeError("Reference send outcome unknown; review before retry") from None
        result = payload.get("result") if isinstance(payload, dict) else None
        if (not r.is_success or not isinstance(result, dict) or result.get("success") is not True
                or result.get("to") != [recipient]):
            raise RuntimeError("Reference send not confirmed; review before retry")


class ReceiptScan:
    def __init__(self, jotform, mail, form_baseline):
        self.jotform, self.mail = jotform, mail
        if type(form_baseline) is not int or form_baseline <= 0:
            raise ValueError("Verified reference form baseline required")
        self.form_baseline = form_baseline

    async def __call__(self, ledger):
        requests = {r["id"]: r for r in ledger.rows() if r["state"] in {"pending", "reply_review", "send_unknown", "exhausted"}}
        if not requests:
            return
        # Complete scan before applying or sending. Failure never means "no receipt".
        submissions = await self.jotform.submissions_after(REFERENCE_FORM, self.form_baseline)
        actions = []
        for submission in submissions:
            if submission.get("status") != "ACTIVE":
                continue
            try:
                token = answer(submission, REQUEST_FIELD)
            except ValueError:
                continue  # old/native/unrelated submissions have no tracking ID
            request = requests.get(token) if isinstance(token, str) else None
            if not request or not (request["last_sent"] or request.get("attempted_at")):
                continue
            try:
                name = answer(submission, "Name of candidate")
                sender = email(answer(submission, "Email address"))
                if name != request["candidate"] or sender != request["referee"]:
                    actions.append((request["id"], "review", "form", submission["id"]))
                else:
                    actions.append((request["id"], "received", "form", submission["id"]))
            except ValueError:
                actions.append((request["id"], "review", "form", submission["id"]))
        now = datetime.now(timezone.utc)
        oldest = min(datetime.fromisoformat(r["created"]) for r in requests.values())
        start = max(oldest, now - timedelta(days=30))
        page = 1
        while page is not None:
            result = await self.mail.search(stamp(start), stamp(now), "[ESCS-REF:", page, 50)
            if result["pageLimitReached"]:
                raise RuntimeError("Receipt scan incomplete; reminder sending is blocked")
            for meta in result["messages"]:
                token_match = re.search(r"\[ESCS-REF:([a-f0-9]{32})\]", str(meta.get("subject", "")))
                request = requests.get(token_match[1]) if token_match else None
                if request:
                    message = await self.mail.get(int(meta["id"]))
                    actions.append((request["id"], email_evidence(message, request), "email", meta["id"]))
            page = result["nextPage"]
        # Confirmed receipt takes precedence over another ambiguous reply in
        # the same scan. Review never overwrites received.
        for request_id, result, kind, evidence_id in actions:
            if result == "review":
                ledger.hold(request_id)
        for request_id, result, kind, evidence_id in actions:
            if result == "received":
                ledger.stop(request_id, kind, evidence_id)


async def ingest_new_sources(ledger, jotform):
    """Only IDs after a fixed, verified activation baseline; no historical enrolment."""
    # No cursor is advanced: restart/replay is deduplicated by (source_id,slot).
    # A production adapter should persist a completed-scan cursor once volume
    # requires it. Reaching the scan cap blocks sends instead of skipping data.
    rows = await jotform.submissions_after(SOURCE_FORM, ledger.baseline)
    schema = await jotform.reference_schema()  # refuse activation without tracking field
    for row in rows:
        if row.get("status") == "ACTIVE":
            ledger.enroll(await jotform.source(str(row["id"])), datetime.now(timezone.utc))
    return schema
