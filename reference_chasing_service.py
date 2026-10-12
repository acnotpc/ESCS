"""Consent-gated reference worker, review queue and protected operator routes."""
import asyncio
import os
from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from recruitment_mail import RecruitmentMail
from reference_chasing import Ledger, Engine, stamp
from reference_chasing import email_evidence
from reference_chasing_adapters import (JotformRead, BitrixSender, ReceiptScan,
                                        ingest_new_sources, form_link)
from reference_monday_sync import MondayReferenceSync, monday_sync_reviews


def on(name):
    return os.getenv(name, "").lower() == "true"


class ChasingService:
    def __init__(self):
        self.engine = None
        self.last_error = None
        self.last_run_at = None
        self.last_success_at = None
        self.last_result = None
        self.run_lock = asyncio.Lock()
        self.monday_sync = {"enabled": False, "writeEnabled": False}

    @property
    def enabled(self):
        return on("ESCS_REFERENCE_CHASING_ENABLED")

    async def run_once(self):
        async with self.run_lock:
            self.last_run_at = stamp(datetime.now(timezone.utc))
            try:
                result = await self._run_once()
            except Exception:
                self.last_error = "Reference scan failed; sends held until a successful scan"
                self.persist_health()
                raise
            if result.get("enabled"):
                self.last_success_at = stamp(datetime.now(timezone.utc))
            self.last_error = None
            self.last_result = result
            self.persist_health()
            return result

    def persist_health(self):
        if self.engine is not None:
            self.engine.ledger.save_health({"lastRunAt": self.last_run_at,
                "lastSuccessAt": self.last_success_at, "lastError": self.last_error,
                "lastResult": self.last_result})

    def review_counts(self):
        if self.engine is None:
            return {}
        ledger = self.engine.ledger
        states = Counter(r["state"] for r in ledger.rows())
        return {"sourceDetails": len(ledger.source_reviews()),
                "unclearReplies": states["reply_review"], "uncertainDelivery": states["send_unknown"],
                "reminderLimit": states["exhausted"], "consentStopped": states["consent_stopped"],
                "boardLinks": len(monday_sync_reviews(ledger))}

    async def sync_monday(self, source):
        # Requires separately approved persistent Monday access. Disabled by
        # default. A board problem cannot undo sends or stop receipt scanning.
        enabled = on("ESCS_REFERENCE_MONDAY_SYNC_ENABLED")
        writes = enabled and on("ESCS_REFERENCE_MONDAY_WRITE_ENABLED")
        self.monday_sync = {"enabled": enabled, "writeEnabled": writes}
        if enabled:
            try:
                adapter = MondayReferenceSync(os.getenv("MONDAY_RECRUITMENT_API_TOKEN", ""), writes)
                self.monday_sync.update(await adapter.run(self.engine.ledger, source))
                self.monday_sync["lastError"] = None
            except Exception:
                self.monday_sync["lastError"] = "Board sync failed; reference processing continues"
            self.monday_sync["lastCheckedAt"] = stamp(datetime.now(timezone.utc))

    async def _run_once(self):
        if not self.enabled:
            return {"enabled": False, "sent": 0}
        # This single-instance service reuses the approved Render persistent disk.
        # No fall-back to ephemeral storage or existing historical consent.
        path = os.getenv("ESCS_REFERENCE_CHASING_DB", "")
        if not path.startswith("/var/data/") or not os.path.isdir("/var/data"):
            raise RuntimeError("Persistent reference tracking storage is not configured")
        source_baseline = int(os.getenv("ESCS_REFERENCE_SOURCE_BASELINE", "0"))
        form_baseline = int(os.getenv("ESCS_REFERENCE_FORM_BASELINE", "0"))
        source = JotformRead(os.getenv("JOTFORM_RECRUITMENT_READ_API_KEY", ""))
        mail = RecruitmentMail(os.getenv("BITRIX_RECRUITMENT_MAIL_REST_URL", ""),
                               int(os.getenv("BITRIX_RECRUITMENT_MAILBOX_ID", "0")))
        writes = on("ESCS_REFERENCE_CHASING_WRITE_ENABLED")
        if writes and not on("ESCS_NATIVE_REFERENCE_WORKFLOW_DISABLED"):
            raise RuntimeError("Native reference workflow must be disabled before live sending")
        if self.engine is None:
            ledger = Ledger(path, source_baseline)
            with ledger.db:
                row = ledger.db.execute("SELECT value FROM settings WHERE key='reference_baseline'").fetchone()
                if row and int(row[0]) != form_baseline:
                    raise RuntimeError("Reference form activation baseline cannot change")
                if form_baseline <= 0:
                    raise RuntimeError("Reference form baseline is not configured")
                ledger.db.execute("INSERT OR IGNORE INTO settings VALUES('reference_baseline',?)", (str(form_baseline),))
            receipts = ReceiptScan(source, mail, form_baseline)
            self.engine = Engine(ledger, source.source, BitrixSender(mail, writes),
                                 None, receipts, write_enabled=writes)
            prior = ledger.health()
            self.last_success_at = self.last_success_at or prior.get("lastSuccessAt")
            if self.last_result is None:
                self.last_result = prior.get("lastResult")
        # Safe defaults/config changes cannot leave an old write-enabled engine running.
        self.engine.write_enabled = writes
        self.engine.sender.enabled = writes
        schema = await ingest_new_sources(self.engine.ledger, source)
        self.engine.form_link = lambda request: form_link(schema, request)
        before = sum(r["sends"] for r in self.engine.ledger.rows())
        preview = await self.engine.tick(datetime.now(timezone.utc))
        after = sum(r["sends"] for r in self.engine.ledger.rows())
        await self.sync_monday(source)
        self.last_error = None
        return {"enabled": True, "writeEnabled": writes, "sent": after - before,
                "dueCount": len(preview), "states": dict(Counter(r["state"] for r in self.engine.ledger.rows())),
                "reviewCounts": self.review_counts(), "mondaySync": self.monday_sync}

    async def loop(self):
        import asyncio
        while True:
            if self.enabled:
                try:
                    await self.run_once()
                except Exception:
                    self.last_error = "Reference scan failed; sends held until a successful scan"
            await asyncio.sleep(300)


def build_chasing_router(service, require_connector_key):
    router = APIRouter(prefix="/reference-chasing", dependencies=[Depends(require_connector_key)])

    @router.get("/status")
    async def status():
        states = Counter(r["state"] for r in service.engine.ledger.rows()) if service.engine else {}
        return {"enabled": service.enabled, "writeEnabled": on("ESCS_REFERENCE_CHASING_WRITE_ENABLED"),
                "states": dict(states), "lastError": service.last_error,
                "lastRunAt": service.last_run_at, "lastSuccessAt": service.last_success_at,
                "lastResult": service.last_result, "reviewCounts": service.review_counts(),
                "mondaySync": service.monday_sync,
                "workerIntervalSeconds": 300, "professionalVerification": "Staff review required"}

    @router.get("/review")
    async def review():
        if service.engine is None:
            return {"sourceReviews": [], "referenceReviews": [], "boardSyncReviews": []}
        ledger = service.engine.ledger
        held = {"reply_review", "send_unknown", "exhausted", "consent_stopped", "source_review"}
        return {"sourceReviews": ledger.source_reviews(),
                "referenceReviews": [r for r in ledger.rows() if r["state"] in held],
                "boardSyncReviews": monday_sync_reviews(ledger)}

    @router.post("/run")
    async def run():
        try:
            return await service.run_once()
        except Exception:
            raise HTTPException(503, "Reference scan failed; sends held") from None

    @router.get("/requests")
    async def requests():
        # Staff-only; names and referee addresses never appear on public MCP.
        return {"requests": service.engine.ledger.rows() if service.engine else []}

    @router.post("/requests/{request_id}/confirm-email/{message_id}")
    async def confirm_email(request_id: str, message_id: int):
        if service.engine is None:
            raise HTTPException(503, "Reference ledger is not initialised")
        ledger = service.engine.ledger
        async with service.engine.lock:
            request = next((r for r in ledger.rows() if r["id"] == request_id), None)
            if request is None:
                raise HTTPException(404, "Reference request not found")
            try:
                message = await service.engine.receipts.mail.get(message_id)
            except Exception:
                raise HTTPException(503, "Email evidence could not be read") from None
            if email_evidence(message, request) == "ignore":
                raise HTTPException(400, "Email does not match this referee/request or is an automatic reply")
            # Calling this protected operator action attests staff have checked
            # the matched email is an actual completed reference. It records
            # receipt only, never professional validity or a hiring decision.
            ledger.stop(request_id, "staff_confirmed_email", str(message_id))
            return {"state": "received", "professionalVerification": "Staff review required"}

    return router
