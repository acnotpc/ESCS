# Conveyance timing reminder trial

The unattended worker is `booking_reminder_worker.py`, started by the application
lifespan. Enable `CONVEYANCE_REMINDERS_ENABLED=true` and
`CONVEYANCE_REMINDERS_WRITE_ENABLED=true` to send. With write disabled it previews
only. It checks once per local hour from 06:00 through 23:00 Europe/London.

The source is the existing operator-verified job/chat bindings and their dated
milestones. Only actual Arrive/Depart D1/D2 events at least 15 minutes old and no
more than 36 hours old are considered, for today/yesterday's bindings. Unknown
crew, multiple bindings for the same booking, missing columns, mismatched job
identity, edited/deleted/ambiguous events or conflicting actuals never generate
field reminders. Start, final return, B2B chain and continuing-bedwatch inference
remain excluded from automated collection; the evaluator below supports manual
reconciliation separately. This worker does not automatically discover or bind
new job chats.

Monday values are freshly read from board 376064564 using the existing account
connection (CONVEYANCE-specific, generic, then existing Monday token fallback).
Only timing columns and Job No are requested; no patient narrative is fetched.
A populated alternative/legacy column suppresses its reminder. No board fields
are written.

Messages go only to Andy's existing Control Room Team personal conversation
(1037), verified before sending. A durable intent is committed first. Message
ID, author, body and timestamp are read back before delivery is recorded. An
uncertain delivery blocks all further sends until reconciled; there is no blind
retry. One consolidated message is allowed per hour and the same missing field
is reminded at most once per local day. A Control Room reply exactly matching
`ACK ESCS-REM-YYYYMMDD-HH` records acknowledgement and suppresses further reminders
for those fields; Monday is still checked and acknowledgement never marks a
field complete.

The protected GET `/conveyance-reminders/status` and POST
`/conveyance-reminders/preview` routes use the existing connector key. Preview
never sends or reserves a delivery. The read-only MCP tool
`get_booking_reminder_status` exposes counts, configuration presence and worker
state without secrets. Existing chats may need their connector tools refreshed
before this new action appears.

Continuing bedwatch: set `ongoing_bedwatch` only from explicit dated operator
evidence; set `pulse_reviewed` after reading the pulse and submitted forms, and
retain `bedwatch_source`. Depart D1 and subsequent job-ending stages are not yet
due while bedwatch continues. Crew-scoped R02/C02A end evidence is separate from
the whole job ending and cannot request completion of the shared booking row.
Conflicting actual job-ending evidence requires crew/job scope reconciliation.
Once an actual bedwatch departure is confirmed and the bedwatch is no longer
ongoing, its missing Depart D1 field can become due. ETAs still do not establish it.

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

The existing `send_bitrix_chat_message` now has an explicit `dialog_id="1037"`
path using `im.message.add` through the existing Andy user webhook. It validates
the ESCS portal and webhook owner 1, then rechecks that the recent personal
conversation belongs to Control Room Team user 1037. Other destinations cannot
use this path. It does not add participants or broaden credentials; existing
group/bot routes remain separate. This sender change still needs live verification
with the next explicitly authorised message. No trial reminder is resent.

Validation: `python -m unittest -q test_conveyance_time_reminders`.

Worker validation: `python -m unittest -q test_booking_reminder_worker`.
