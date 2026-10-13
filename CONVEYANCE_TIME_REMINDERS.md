# Conveyance timing reminder trial

This is a draft evaluator and durable reminder ledger. It is not wired to a
background worker, Monday reader or Bitrix sender. Recurring sending remains off.

Use `evaluate(jobs, now)` with verified booking IDs, crew IDs and timezone-aware
stage evidence. Preserve the source of each fact. A blank time-only board field,
an ETA, a bracketed availability time or a job marked complete does not by itself
establish final return, duty start or a missed journey stage. Request a field only
when a dated, actual event confirms its stage has already happened. The legacy
and newer D2 arrival fields are alternatives, so a populated legacy cell is not
chased as missing merely because its newer counterpart is blank.

For verified B2B links, one job's handover/end becomes the following job's start.
Keep the first duty start across the entire chain. Missing original starts,
unverified crew continuity, conflicting handovers and cycles require review.
Crew changes must have separate dated evidence. The booking's original date must
not be copied onto undated events in a multi-day job. All results remain
`dispatch_ready=false`; these checks do not clear rest or driver/vehicle status.

Use `ReminderLedger.reserve(run_id, findings)` before sending one consolidated
message. The caller must use a stable run ID for the trial and an existing
restricted destination verified before the send. A durable intent blocks resend
after a restart or uncertain response; changes in findings do not reset it.
Call `confirm_sent` only after reading back the sent message and confirming its
ID and timestamp. Track confirmation and verified board updates independently
with `elapsed`; a reply alone does not resolve a missing field.

No retry/recurrence is configured. Before activation, add the authenticated
source adapter, due-stage/date/crew reconciliation, bounded reminder cadence,
operator acknowledgement handling and a read-back-verified sender. The current
Bitrix connector can read the operator's direct conversation but rejected the
trial send; the interactive Andy browser session sent it successfully. Resolve
the direct-message sender addressing/permissions before enabling unattended sends.

Validation: `python -m unittest -q test_conveyance_time_reminders`.
