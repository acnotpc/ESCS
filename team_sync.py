"""Bitrix chat -> existing teams feed post. Disabled until explicitly configured.

No patient text is stored or published. Only allowlisted operational milestones
are extracted; unknown, edited or deleted updates require review.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

LONDON = ZoneInfo("Europe/London")
START = "[ESCS LIVE TEAM STATUS]"
END = "[/ESCS LIVE TEAM STATUS]"
LABELS = {
    "arrived_d1": "At D1", "departed_d1": "Travelling to D2",
    "arrived_d2": "At D2 — handover/waiting", "departed_d2": "Departed D2 — release unconfirmed",
    "returned_d1": "Returned to D1", "departed_return_d1": "Returning to meeting point",
    "returned_meeting": "Returned to meeting point — release unconfirmed",
    "released": "Crew release reported — check commitments before allocation",
    "complete": "Job complete reported — release unconfirmed",
    "review": "Chat update needs control-room review",
}
# Whole-line matching avoids treating historical reports, quoted instructions,
# negation, ETA predictions or arbitrary narrative as an actual milestone.
PATTERNS = {
    "arrived_d1": r"(?:arrived(?: at)?|at)\s+d1",
    "departed_d1": r"(?:departed|depart|left)\s+d1",
    "arrived_d2": r"(?:arrived(?: at)?|at)\s+d2",
    "departed_d2": r"(?:departed|depart|left)\s+d2",
    "returned_d1": r"(?:returned to|back at)\s+d1",
    "departed_return_d1": r"(?:departed|left)\s+d1\s+(?:return|rtn)",
    "returned_meeting": r"(?:returned to|back at)\s+(?:the\s+)?meeting point",
    "released": r"(?:crew|team)\s+(?:released|free|available)",
    "complete": r"(?:job\s+)?complete(?:d)?",
}


def aware(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return dt


def operational_update(text, sent_at, now=None):
    """Return sanitized milestone. Time-only lines use message's London date.

    A prior-day time is ambiguous: require explicit ISO date or manual review.
    """
    sent = aware(sent_at).astimezone(LONDON)
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if len(lines) != 1:
        return "review", sent.isoformat()
    line = re.sub(r"\s+-\s+", " ", lines[0].lower().replace("#", "")).strip().rstrip(".")
    match = re.match(r"^(?:(\d{1,2}:\d{2}|\d{4})\s*[-–:]?\s*)?(.+?)(?:\s+(\d{1,2}:\d{2}|\d{4}))?$", line)
    if not match:
        return "review", sent.isoformat()
    before, content, after = match.groups()
    if before and after:
        return "review", sent.isoformat()
    status = next((key for key, pattern in PATTERNS.items() if re.fullmatch(pattern, content)), "review")
    stamp = sent
    clock = before or after
    if clock:
        digits = clock.replace(":", "")
        try:
            stamp = sent.replace(hour=int(digits[:-2]), minute=int(digits[-2:]), second=0, microsecond=0)
        except ValueError:
            return "review", sent.isoformat()
        if stamp > sent + timedelta(minutes=2):
            return "review", sent.isoformat()
    if stamp > (now or datetime.now(timezone.utc)) + timedelta(minutes=2):
        return "review", sent.isoformat()
    return status, stamp.isoformat()


def rest_summary(first_meeting, final_return=None, now=None):
    start = aware(first_meeting)
    end = aware(final_return) if final_return else (now or datetime.now(timezone.utc))
    hours = (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() / 3600
    if hours < 0:
        raise ValueError("Return precedes meeting")
    # Add elapsed hours in UTC so clocks changing do not shorten the rest period.
    earliest = (end.astimezone(timezone.utc) + timedelta(hours=11)).astimezone(LONDON) if final_return and hours > 12 else None
    return {"duty_hours": round(hours, 2), "rest_required": hours > 12,
            "earliest_next_meeting": earliest.isoformat() if earliest else None,
            "accommodation_offer_required": hours > 16}


def replace_status_block(original, lines):
    block = START + "\n" + "\n".join(lines) + "\n" + END
    if START not in original and END not in original:
        return original.rstrip() + "\n\n" + block
    if original.count(START) != 1 or original.count(END) != 1 or original.index(START) >= original.index(END):
        raise ValueError("Ambiguous status block; preserve post and request review")
    left = original.index(START)
    right = original.index(END) + len(END)
    return original[:left] + block + original[right:]


class Settings:
    def __init__(self):
        self.enabled = os.getenv("BITRIX_TEAM_SYNC_ENABLED") == "true"
        self.live = os.getenv("BITRIX_TEAM_SYNC_WRITE_ENABLED") == "true"
        self.domain = "escs.bitrix24.com"
        self.token = os.getenv("BITRIX_EVENT_APPLICATION_TOKEN", "")
        self.event_mode = os.getenv("BITRIX_TEAM_EVENT_MODE", "webhook")
        # Migration: the existing secret was the registered bot token, not the
        # callback application token. It remains usable for authenticated fetch.
        self.bot_token = os.getenv("BITRIX_TEAM_BOT_TOKEN", self.token)
        self.bot_id = os.getenv("BITRIX_TEAM_BOT_ID", "")
        self.rest_url = os.getenv("BITRIX_REST_WEBHOOK_URL", "")
        self.db_path = os.getenv("BITRIX_TEAM_STATE_DB", "")

    def validate(self):
        credential = self.bot_token if self.event_mode == "fetch" else self.token
        if self.event_mode not in {"fetch", "webhook"} or not self.enabled or not credential or not self.bot_id or not self.rest_url or not self.db_path:
            raise HTTPException(503, "Teams sync is disabled or setup is incomplete")
        parsed = urlsplit(self.rest_url)
        if parsed.scheme != "https" or parsed.hostname != self.domain or not re.fullmatch(r"/rest/\d+/[^/]+/?", parsed.path) or parsed.query or parsed.fragment:
            raise HTTPException(503, "Bitrix API destination is invalid")
        if not Path(self.db_path).is_absolute():
            raise HTTPException(503, "Persistent state path must be absolute")


class Binding(BaseModel):
    chat_id: int = Field(gt=0)
    job_number: str = Field(pattern=r"^JOB\d{5}$")
    monday_item_id: int = Field(gt=0)
    post_id: int = Field(gt=0)
    service_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    team_label: str = Field(pattern=r"^Team [A-Z]$")
    vehicle: str = Field(pattern=r"^[A-Z0-9]{5,8}$")
    # Authoritative first meeting for EACH member; do not reset between jobs.
    first_meetings: dict[str, str]
    allowed_author_ids: list[int] = Field(min_length=1)

    def checked(self):
        datetime.strptime(self.service_date, "%Y-%m-%d")
        if not self.first_meetings:
            raise ValueError("Crew and first meetings required")
        for sid, stamp in self.first_meetings.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid):
                raise ValueError("Use canonical staff IDs")
            aware(stamp)
        return self


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS bindings(chat_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS milestones(chat_id INTEGER, message_id INTEGER, revision INTEGER,
            status TEXT, occurred TEXT, PRIMARY KEY(chat_id,message_id));
          CREATE TABLE IF NOT EXISTS pending(post_id INTEGER PRIMARY KEY, last_error TEXT);
          CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, post_id INTEGER, written_at TEXT, digest TEXT);
          CREATE TABLE IF NOT EXISTS queue_cursors(bot_id TEXT PRIMARY KEY, offset INTEGER NOT NULL);
        """)

    def bind(self, binding):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO bindings VALUES (?,?)", (binding.chat_id, binding.model_dump_json()))

    def binding(self, chat_id):
        row = self.db.execute("SELECT data FROM bindings WHERE chat_id=?", (chat_id,)).fetchone()
        return Binding.model_validate_json(row[0]) if row else None

    def accept(self, binding, message_id, revision, status, occurred):
        old = self.db.execute("SELECT revision,status,occurred FROM milestones WHERE chat_id=? AND message_id=?", (binding.chat_id, message_id)).fetchone()
        if old and (revision < old[0] or (revision == old[0] and (status, occurred) == (old[1], old[2]))):
            return False
        if old and revision == old[0]:
            # Same revision with differing payloads is ambiguous, never release.
            status = "review"
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO milestones VALUES (?,?,?,?,?)", (binding.chat_id, message_id, revision, status, occurred))
            self.db.execute("INSERT OR IGNORE INTO pending VALUES (?,NULL)", (binding.post_id,))
        return True

    def reconcile(self, chat_id, message_id, status, occurred):
        if status not in LABELS or status == "review":
            raise ValueError("Select a verified operational milestone")
        if aware(occurred) > datetime.now(timezone.utc):
            raise ValueError("Milestone has not happened")
        binding = self.binding(chat_id)
        if not binding:
            raise ValueError("Unmapped chat")
        with self.db:
            changed = self.db.execute("UPDATE milestones SET status=?,occurred=? WHERE chat_id=? AND message_id=? AND status='review'", (status, occurred, chat_id, message_id)).rowcount
            if not changed:
                raise ValueError("No outstanding review for this message")
            self.db.execute("INSERT OR IGNORE INTO pending VALUES (?,NULL)", (binding.post_id,))
            self.db.execute("INSERT INTO audit(post_id,written_at,digest) VALUES (?,?,?)", (binding.post_id, datetime.now(timezone.utc).isoformat(), "operator-reconciled-milestone"))

    def render(self, post_id):
        lines = []
        for row in self.db.execute("SELECT data FROM bindings ORDER BY chat_id"):
            b = Binding.model_validate_json(row[0])
            if b.post_id != post_id:
                continue
            # Never write a prior day's team list based on late events automatically.
            if b.service_date != datetime.now(LONDON).date().isoformat():
                raise ValueError("Historical teams list requires review")
            records = self.db.execute("SELECT * FROM milestones WHERE chat_id=?", (b.chat_id,)).fetchall()
            if not records:
                continue
            latest = max(records, key=lambda x: (aware(x["occurred"]).timestamp(), x["revision"], x["message_id"]))
            # Any uncertain edit/delete remains visible until manually reconciled.
            status = "review" if any(x["status"] == "review" for x in records) else latest["status"]
            stamp = aware(latest["occurred"]).astimezone(LONDON).strftime("%H:%M")
            line = f"{b.team_label} | {b.job_number} | {b.vehicle} | {LABELS[status]} | {stamp}"
            if status in ("returned_meeting", "released"):
                # Released elsewhere does not establish the final meeting return.
                returns = [x for x in records if x["status"] == "returned_meeting"]
                final = max(returns, key=lambda x: aware(x["occurred"]).timestamp())["occurred"] if returns else None
            else:
                final = None
            summaries = [rest_summary(start, final) for start in b.first_meetings.values()]
            earliest = [x["earliest_next_meeting"] for x in summaries if x["earliest_next_meeting"]]
            if earliest:
                line += " | Earliest next meeting after rest: " + max(aware(x) for x in earliest).astimezone(LONDON).strftime("%d %b %H:%M")
            elif any(x["rest_required"] for x in summaries):
                line += " | 11h rest required after confirmed final meeting return"
            if any(x["accommodation_offer_required"] for x in summaries):
                line += " | Over 16h: offer accommodation; review fatigue and driving"
            lines.append(line)
        return lines


async def api_call(settings, method, params):
    # The callback's auth/endpoint is never used for outbound calls.
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        response = await client.post(settings.rest_url.rstrip("/") + "/" + method, json=params)
    if not response.is_success:
        raise RuntimeError("Bitrix request failed")
    payload = response.json()
    if "error" in payload:
        raise RuntimeError("Bitrix rejected request")
    return payload["result"]


class Synchronizer:
    def __init__(self, settings, call=api_call):
        self.settings = settings
        self.call = call
        self.store = None
        self.lock = asyncio.Lock()
        self.poll_lock = asyncio.Lock()
        self.last_poll = None
        self.poll_error = None

    def ready(self):
        self.settings.validate()
        if self.store is None:
            self.store = Store(self.settings.db_path)
        return self.store

    async def flush(self):
        async with self.lock:
            await self._flush()

    async def _flush(self):
            store = self.ready()
            for row in store.db.execute("SELECT post_id FROM pending").fetchall():
                post_id = row[0]
                try:
                    lines = store.render(post_id)
                    if not self.settings.live:
                        continue
                    posts = await self.call(self.settings, "log.blogpost.get", {"POST_ID": post_id})
                    post = next(x for x in posts if int(x["ID"]) == post_id)
                    before = post["DETAIL_TEXT"]
                    after = replace_status_block(before, lines)
                    if before != after:
                        # Detect manual edits between the initial read and mutation.
                        fresh = await self.call(self.settings, "log.blogpost.get", {"POST_ID": post_id})
                        if next(x for x in fresh if int(x["ID"]) == post_id) != post:
                            raise ValueError("Post changed; retry from current version")
                        await self.call(self.settings, "log.blogpost.update", {"POST_ID": post_id, "POST_TITLE": post["TITLE"], "POST_MESSAGE": after})
                    # API has no compare-and-swap: keep one instance and a single writer.
                    verified = await self.call(self.settings, "log.blogpost.get", {"POST_ID": post_id})
                    if next(x for x in verified if int(x["ID"]) == post_id)["DETAIL_TEXT"] != after:
                        raise ValueError("Post verification failed")
                    with store.db:
                        store.db.execute("DELETE FROM pending WHERE post_id=?", (post_id,))
                        store.db.execute("INSERT INTO audit(post_id,written_at,digest) VALUES (?,?,?)", (post_id, datetime.now(timezone.utc).isoformat(), hashlib.sha256(after.encode()).hexdigest()))
                except Exception:
                    with store.db:
                        store.db.execute("UPDATE pending SET last_error=? WHERE post_id=?", ("Update requires retry or control-room review", post_id))

    async def event(self, payload):
        if self.settings.event_mode != "webhook":
            raise HTTPException(503, "Webhook receiver is disabled in fetch mode")
        store = self.ready()
        auth = payload.get("auth", {})
        if auth.get("domain") != self.settings.domain or not secrets.compare_digest(str(auth.get("application_token", "")), self.settings.token):
            logging.getLogger("escs.team_sync").warning(
                "Bitrix callback rejected: domain_matches=%s token_present=%s token_matches=%s",
                auth.get("domain") == self.settings.domain,
                bool(auth.get("application_token")),
                secrets.compare_digest(str(auth.get("application_token", "")), self.settings.token),
            )
            raise HTTPException(401, "Invalid event authentication")
        return await self._process_event(payload)

    async def poll_once(self):
        """Consume the authenticated Bitrix queue; acknowledge after persistence.

        Only this worker uses the cursor. No incoming HTTP request can reach this
        trusted path. Message text and tokens are never retained in the cursor.
        """
        if self.settings.event_mode != "fetch":
            return False
        async with self.poll_lock:
            store = self.ready()
            row = store.db.execute("SELECT offset FROM queue_cursors WHERE bot_id=?", (self.settings.bot_id,)).fetchone()
            offset = row[0] if row else None
            params = {"botId": int(self.settings.bot_id), "botToken": self.settings.bot_token,
                      "limit": 100, "withUserEvents": False}
            if offset is not None:
                params["offset"] = offset
            try:
                result = await self.call(self.settings, "imbot.v2.Event.get", params)
                events = result["events"]
                next_offset = int(result["nextOffset"])
                if not isinstance(events, list) or next_offset < (offset or 0):
                    raise ValueError("Invalid queue cursor")
                last_id = (offset or 0) - 1
                for entry in events:
                    event_id = int(entry["eventId"])
                    if event_id <= last_id or event_id >= next_offset:
                        raise ValueError("Invalid queue ordering")
                    sent = aware(entry["date"])
                    payload = {"event": entry["type"], "ts": int(sent.timestamp()), "data": entry["data"]}
                    try:
                        await self._process_event(payload, queue_revision=event_id)
                    except HTTPException as exc:
                        # The author allowlist intentionally excludes unrelated
                        # participants. Other structure/bot errors stop the queue.
                        if exc.status_code != 403 or exc.detail != "Unexpected author":
                            raise
                    last_id = event_id
                async with self.lock:
                    with store.db:
                        store.db.execute("INSERT OR REPLACE INTO queue_cursors VALUES (?,?)", (self.settings.bot_id, next_offset))
                self.last_poll = datetime.now(timezone.utc).isoformat()
                self.poll_error = None
                return bool(result.get("hasMore"))
            except Exception:
                self.poll_error = "Event queue requires retry or control-room review"
                raise RuntimeError(self.poll_error) from None

    async def _process_event(self, payload, queue_revision=None):
        store = self.ready()
        data = payload.get("data", {})
        if str(data.get("bot", {}).get("id", "")) != self.settings.bot_id:
            raise HTTPException(403, "Unexpected bot")
        event = payload.get("event")
        if event not in {"ONIMBOTV2MESSAGEADD", "ONIMBOTV2MESSAGEUPDATE", "ONIMBOTV2MESSAGEDELETE"}:
            return {"accepted": False, "reason": "irrelevant event"}
        try:
            chat_id = int(data["chat"]["id"])
            binding = store.binding(chat_id)
            if not binding:
                return {"accepted": False, "reason": "unmapped chat"}
            user = data.get("user", {})
            if int(user.get("id", 0)) not in binding.allowed_author_ids or str(user.get("bot", "0")).lower() in ("true", "1"):
                raise HTTPException(403, "Unexpected author")
            if event == "ONIMBOTV2MESSAGEDELETE":
                message_id = int(data["messageId"])
                occurred = datetime.fromtimestamp(int(payload["ts"]), timezone.utc).isoformat()
                status = "review"
            else:
                message = data["message"]
                message_id = int(message["id"])
                if int(message.get("chatId", chat_id)) != chat_id:
                    raise ValueError("Chat mismatch")
                if str(message.get("isSystem", "0")).lower() in ("true", "1"):
                    return {"accepted": False, "reason": "system message"}
                status, occurred = operational_update(str(message.get("text", "")), message["date"])
                if event == "ONIMBOTV2MESSAGEUPDATE":
                    # Edits may retract a safety-critical release/return milestone.
                    status = "review"
            event_stamp = int(payload["ts"])
            revision = event_stamp if queue_revision is None else queue_revision
            if event_stamp > int(datetime.now(timezone.utc).timestamp()) + 120:
                raise ValueError("Future event")
        except (KeyError, TypeError, ValueError, OverflowError):
            raise HTTPException(400, "Invalid event structure") from None
        if event == "ONIMBOTV2MESSAGEADD" and status == "review" and not re.search(r"\b(arriv\w*|depart\w*|d1|d2|meeting|releas\w*|available|complete\w*)\b", str(data.get("message", {}).get("text", "")), re.I):
            return {"accepted": False, "reason": "no operational milestone"}
        async with self.lock:
            accepted = store.accept(binding, message_id, revision, status, occurred)
            await self._flush()
        return {"accepted": accepted}


def inflate_form(items):
    """Decode Bitrix's PHP-style URL-encoded keys. Never log the payload."""
    result = {}
    for key, value in items:
        parts = re.findall(r"[^\[\]]+", key)
        if not parts or len(parts) > 8:
            raise ValueError("Invalid form key")
        target = result
        for part in parts[:-1]:
            target = target.setdefault(part, {})
            if not isinstance(target, dict):
                raise ValueError("Conflicting form keys")
        if parts[-1] in target:
            raise ValueError("Duplicate form key")
        target[parts[-1]] = value
    return result


def build_router(sync, authorize):
    router = APIRouter(prefix="/team-sync")

    @router.post("/bitrix-events")
    async def events(request: Request):
        if not sync.settings.enabled:
            raise HTTPException(503, "Teams sync is disabled")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                raise HTTPException(413, "Event too large")
        try:
            if request.headers.get("content-type", "").startswith("application/json"):
                payload = json.loads(raw)
            else:
                from urllib.parse import parse_qsl
                payload = inflate_form(parse_qsl(raw.decode(), keep_blank_values=True, max_num_fields=256))
            if not isinstance(payload, dict):
                raise ValueError("Invalid object")
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid event body") from None
        return await sync.event(payload)

    @router.put("/bindings", dependencies=[Depends(authorize)])
    async def bind(binding: Binding):
        try:
            binding.checked()
        except ValueError:
            raise HTTPException(422, "Invalid crew meeting data") from None
        async with sync.lock:
            sync.ready().bind(binding)
        return {"saved": True, "chat_id": binding.chat_id}

    @router.post("/reconcile", dependencies=[Depends(authorize)])
    async def reconcile(chat_id: int, message_id: int, status: str, occurred: str):
        try:
            async with sync.lock:
                sync.ready().reconcile(chat_id, message_id, status, occurred)
                await sync._flush()
        except ValueError:
            raise HTTPException(422, "Reconciliation requires an existing review and a verified past milestone") from None
        return {"reconciled": True}

    @router.get("/status", dependencies=[Depends(authorize)])
    async def status():
        if not sync.settings.enabled:
            return {"enabled": False, "write_enabled": False}
        store = sync.ready()
        return {"enabled": True, "write_enabled": sync.settings.live,
                "event_mode": sync.settings.event_mode,
                "last_poll": sync.last_poll, "poll_error": sync.poll_error,
                "bindings": store.db.execute("SELECT COUNT(*) FROM bindings").fetchone()[0],
                "pending_posts": [dict(x) for x in store.db.execute("SELECT * FROM pending")],
                "reviews": [dict(x) for x in store.db.execute(
                    "SELECT chat_id,message_id,occurred FROM milestones WHERE status='review' ORDER BY chat_id,message_id")]}

    @router.post("/retry", dependencies=[Depends(authorize)])
    async def retry():
        await sync.flush()
        return {"retried": True}

    @router.get("/preview/{post_id}", dependencies=[Depends(authorize)])
    async def preview(post_id: int):
        return {"lines": sync.ready().render(post_id)}

    return router
