"""Draft per-referee reminder engine. Adapters/activation are deliberately separate."""
import asyncio
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
from html.parser import HTMLParser
from html import escape

SOURCE_FORM = "92583319416361"
REFERENCE_FORM = "51517384496868"
CONSENT = "May we approach both referees you have listed for references now, including your most recent employer?"
REFEREES = ("Email address of most recent employer - 1st reference", "Email address of 2nd reference")
JOBS = "jobs@securecareservices.co.uk"
INTERVAL = timedelta(days=3)


def request_body(candidate, link):
    """Bitrix mail bodies are HTML; make the primary action an explicit link."""
    name, url = escape(candidate), escape(link, quote=True)
    return (
        '<p>Hello,</p><p>Please provide your reference for <strong>' + name +
        '</strong>.</p><p>The candidate has given ESCS permission to approach you.</p>'
        '<p><a href="' + url + '" style="display:inline-block;padding:14px 22px;'
        'background:#176c48;color:#ffffff;text-decoration:none;font-weight:bold;'
        'border-radius:4px">Complete the reference form</a></p>'
        '<p>If the button does not open, copy this address into your browser:<br>'
        '<a href="' + url + '">' + url + '</a></p>'
        '<p>You may instead reply with the completed reference, keeping the reference ID '
        'in the subject. If you cannot provide a reference, please reply so the Recruitment '
        'Team can review.</p><p>ESCS Recruitment Team<br>'
        '<a href="mailto:jobs@securecareservices.co.uk">jobs@securecareservices.co.uk</a></p>'
    )


def stamp(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware timestamp required")
    return value.astimezone(timezone.utc).isoformat()


def email(value):
    if not isinstance(value, str) or not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", value.strip()):
        raise ValueError("Invalid referee email")
    return value.strip().lower()


def answer(submission, label):
    answers = submission.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("Missing canonical form answers")
    matches = [v.get("answer") for v in answers.values()
               if isinstance(v, dict) and v.get("text") == label]
    if len(matches) != 1:
        raise ValueError("Form field is missing or ambiguous")
    return matches[0]


def canonical(submission):
    """Only canonical Jotform API responses may be passed here, not inbound payloads."""
    if str(submission.get("form_id")) != SOURCE_FORM or submission.get("status") != "ACTIVE":
        raise ValueError("Wrong or inactive source submission")
    sid = str(submission.get("id", ""))
    if not sid.isdigit():
        raise ValueError("Invalid source submission ID")
    if answer(submission, CONSENT) != "Yes":
        return None
    name = answer(submission, "Name")
    if not isinstance(name, dict):
        raise ValueError("Expected a canonical full-name answer")
    first, last = name.get("first", ""), name.get("last", "")
    if not isinstance(first, str) or not isinstance(last, str):
        raise ValueError("Invalid candidate name")
    display = (first.strip() + " " + last.strip()).strip()
    if not display or len(display) > 200 or any(ord(c) < 32 for c in display):
        raise ValueError("Invalid candidate name")
    refs = [email(answer(submission, label)) for label in REFEREES]
    if refs[0] == refs[1] or JOBS in refs:
        raise ValueError("Two distinct external referees are required")
    return {"source_id": sid, "name": display, "referees": refs}


class DirectText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocked = 0
        self.stack = []
        self.quote_started = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        marker = (attrs.get("class", "") + " " + attrs.get("id", "")).lower()
        if any(value in marker for value in ("gmail_quote", "yahoo_quoted", "divrplyfwdmsg")):
            self.quote_started = True
        blocked = tag in {"blockquote", "script", "style"}
        if tag not in {"br", "img", "hr", "input", "meta", "link"}:
            self.stack.append((tag, blocked))
        if blocked:
            self.blocked += 1
        if tag in {"p", "div", "br"} and not self.blocked and not self.quote_started:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                self.blocked -= sum(int(blocked) for _, blocked in self.stack[i:])
                del self.stack[i:]
                break

    def handle_data(self, data):
        if not self.blocked and not self.quote_started:
            self.parts.append(data)


def email_evidence(message, request):
    """Receipt evidence only. Never evaluates suitability/professional validity."""
    if str(message.get("mailboxEmail", "")).lower() != JOBS:
        return "ignore"
    senders = {a.lower() for _, a in getaddresses([str(message.get("from", ""))])}
    recipients = {a.lower() for _, a in getaddresses([str(message.get("to", ""))])}
    token = "[ESCS-REF:" + request["id"] + "]"
    if senders != {request["referee"]} or JOBS not in recipients or token not in str(message.get("subject", "")):
        return "ignore"
    try:
        observed = datetime.fromisoformat(str(message.get("date", "")).replace("Z", "+00:00"))
        if not (request.get("last_sent") or request.get("attempted_at")) or stamp(observed) < request["created"]:
            return "ignore"
    except ValueError:
        return "review"
    body = message.get("body")
    if not isinstance(body, str) or len(body) > 24000:
        return "review"
    parser = DirectText()
    parser.feed(body)
    text = "".join(parser.parts)
    text = re.split(r"(?im)^\s*(?:on .+wrote:|from:|[- ]*original message[- ]*)", text)[0].lower()
    subject = str(message.get("subject", "")).lower()
    if re.search(r"out of (?:the )?office|automatic reply|auto.reply|on (?:annual )?leave", subject + " " + text):
        return "ignore"
    if re.search(r"\b(?:not|unable|cannot|can't|will|later|tomorrow)\b", text):
        return "review"
    # Explicit attached-reference response from the nominated sender, using
    # the request ID, may stop chasing. The document still needs staff review.
    if message.get("hasAttachments") is True and re.search(
            r"(?:attached.{0,80}(?:completed )?reference|(?:completed )?reference.{0,80}attached)", text):
        return "received"
    return "review"


class Ledger:
    """One instance/writer on the existing persistent disk. No email bodies stored."""
    def __init__(self, path, baseline):
        if type(baseline) is not int or baseline <= 0:
            raise ValueError("Verified pre-activation source submission baseline required")
        self.baseline = baseline
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS requests(
            id TEXT PRIMARY KEY,source_id TEXT NOT NULL,slot INTEGER NOT NULL,
            candidate TEXT NOT NULL,referee TEXT NOT NULL,created TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',sends INTEGER NOT NULL DEFAULT 0,
            last_sent TEXT,attempted_at TEXT,due TEXT NOT NULL,evidence_kind TEXT,evidence_id TEXT,
            UNIQUE(source_id,slot));
        """)
        prior = self.db.execute("SELECT value FROM settings WHERE key='baseline'").fetchone()
        if prior and int(prior[0]) != baseline:
            raise ValueError("Activation baseline cannot change on restart")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO settings VALUES('baseline',?)", (str(baseline),))
            # A lost send result must never be automatically re-sent.
            self.db.execute("UPDATE requests SET state='send_unknown' WHERE state='sending'")

    def rows(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM requests ORDER BY created,id")]

    def enroll(self, submission, now):
        record = canonical(submission)
        if record is None or int(record["source_id"]) <= self.baseline:
            return []
        created = stamp(now)
        with self.db:
            for slot, referee in enumerate(record["referees"]):
                existing = self.db.execute("SELECT * FROM requests WHERE source_id=? AND slot=?",
                                           (record["source_id"], slot)).fetchone()
                if existing and (existing["referee"] != referee or existing["candidate"] != record["name"]):
                    raise ValueError("Existing nomination changed; review before creating replacement")
                self.db.execute("INSERT OR IGNORE INTO requests(id,source_id,slot,candidate,referee,created,due) VALUES(?,?,?,?,?,?,?)",
                                (secrets.token_hex(16), record["source_id"], slot, record["name"], referee, created, created))
        return [r for r in self.rows() if r["source_id"] == record["source_id"]]

    def stop(self, request_id, kind, evidence_id):
        if kind not in {"form", "email", "staff_confirmed_email"} or not str(evidence_id).strip():
            raise ValueError("Confirmed receipt evidence is required")
        with self.db:
            self.db.execute("UPDATE requests SET state='received',evidence_kind=?,evidence_id=? WHERE id=? AND state!='received'",
                            (kind, str(evidence_id), request_id))

    def hold(self, request_id):
        with self.db:
            self.db.execute("UPDATE requests SET state='reply_review' WHERE id=? AND state='pending'", (request_id,))


class Engine:
    """No timers or live adapters are installed by importing this draft module."""
    def __init__(self, ledger, source, sender, form_link, receipts, write_enabled=False):
        self.ledger, self.source, self.sender = ledger, source, sender
        self.form_link, self.receipts = form_link, receipts
        self.write_enabled = write_enabled
        self.lock = asyncio.Lock()

    async def tick(self, now):
        async with self.lock:
            # Receipt adapter must finish all relevant pages; an error prevents
            # every send for this tick. It must verify form request IDs and email
            # evidence before calling ledger.stop/hold. No guessed name matches.
            await self.receipts(self.ledger)
            preview = []
            for request in self.ledger.rows():
                if request["state"] != "pending" or request["due"] > stamp(now):
                    continue
                verified = canonical(await self.source(request["source_id"]))
                if (verified is None or verified["source_id"] != request["source_id"]
                        or verified["referees"][request["slot"]] != request["referee"]
                        or verified["name"] != request["candidate"]):
                    with self.ledger.db:
                        self.ledger.db.execute("UPDATE requests SET state='consent_stopped' WHERE id=?", (request["id"],))
                    continue
                if request["sends"] >= 4:
                    with self.ledger.db:
                        self.ledger.db.execute("UPDATE requests SET state='exhausted' WHERE id=?", (request["id"],))
                    continue
                preview.append({"request_id": request["id"], "reminder": request["sends"] > 0})
                if not self.write_enabled:
                    continue
                # Persist intent before network. Timeout/unknown outcome is held
                # for reconciliation, never an automatic duplicate send.
                with self.ledger.db:
                    self.ledger.db.execute("UPDATE requests SET state='sending',attempted_at=? WHERE id=? AND state='pending'", (stamp(now), request["id"]))
                subject = ("ESCS reference request reminder " if request["sends"] else "ESCS reference request ")
                subject += "[ESCS-REF:" + request["id"] + "]"
                link = self.form_link(request)
                body = request_body(request["candidate"], link)
                try:
                    # Adapter must pin jobs@ sender, one exact recipient and
                    # confirm success with the provider. No generic send tool.
                    await self.sender(request["referee"], subject, body)
                except Exception:
                    with self.ledger.db:
                        self.ledger.db.execute("UPDATE requests SET state='send_unknown' WHERE id=?", (request["id"],))
                    continue
                sent = stamp(now)
                with self.ledger.db:
                    self.ledger.db.execute("UPDATE requests SET state='pending',sends=sends+1,last_sent=?,due=? WHERE id=?",
                                           (sent, stamp(now + INTERVAL), request["id"]))
            return preview
