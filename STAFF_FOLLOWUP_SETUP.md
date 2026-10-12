# Staff administrative follow-up

Board: https://exclusive-gang.monday.com/boards/424237712

## Staff operation

Select the required items in **HR follow-up requested**. The dedicated selector
is blank on existing records; missing or historic statuses do not automatically
start a campaign. The worker checks every five minutes and sends one combined
email for that record. Pre-employment records use jobs@securecareservices.co.uk;
employed and employee-pending-paperwork records use hr@securecareservices.co.uk.
Before pre-employment medical requests, the existing pre-employment-checks-sent
date must be present. Unsupported stages, mismatched groups/statuses, absent or
changed emails and changed request selections pause for staff review.

History, medical and working-time links carry a stable random opaque request ID.
Canonical signed submissions to the correct form stop only the matching item.
Either valid 48-hour choice is accepted; cancellation also goes to staff review.
Unknown IDs cannot stop an item and no name matching is used.

Record other received information in **HR follow-up received** after verifying
the correct record. For right to work, this includes evidence received or a check
appointment arranged. For DBS, confirm all requested application information has
been received through the agreed secure route. These entries stop chasing but
never approve identity, right to work, DBS or medical suitability. A generic
new-starter form is not automatically treated as a complete DBS application.

**HR follow-up update** shows outstanding items, received count, email count,
next reminder time or the need for staff review. It contains no medical answers,
identity document numbers or signature URLs. Existing checklist/approval/status
fields and existing expiry/renewal automations are not changed by this worker.
References continue in their separate consent-gated worker; this selector never
contacts a referee or grants permission to approach one.

Initial email plus at most three reminders, 72 hours apart, shared across all
selected items. Each received item disappears from later emails. All received
items stop the campaign and leave staff approval/review separate. Exhaustion,
unknown delivery, process interruption during a reserved send, routing changes,
withdrawal, rejection, leaver or archived state hold the campaign for staff review.
Removing a selection pauses the campaign. This first version has one durable
campaign per board record: changing selections or requesting a new cycle after
completion requires staff review, not automatic reopening. Existing annual
renewal/expiry rules continue separately.

## Deployed implementation

`checklist_workflow.py` is wired into application startup and shutdown, with a
five-minute loop and a SQLite ledger on the persistent disk at
/var/data/recruitment-checklist.sqlite. It validates fixed board/column types and
label IDs, rechecks the canonical staff record and receipts immediately before
reserving a send, stores routing/receipt IDs and state, and never automatically
retries an uncertain delivery. API errors and logs do not expose credentials or
medical answers. Complete paginated reads are required; incomplete or failed
receipt scans block sends. Baselines are persisted before issuing request links.

The only writable column is long_text_mm82ms8e (HR follow-up update). Controls:
request dropdown_mm82vwzc; staff receipt dropdown_mm82k2cm. Protected health is
GET /recruitment-checklist/workflow/status using the existing connector key.
No public HR send API is added.

The startup connection probe uses Andrew's previously selected item 1005891310.
When no items are selected it writes only a connection note to the new progress
column, requests no information and sends no email. It does not edit his existing
employment/checklist fields.

## Verification

157 local tests passed covering combined send, partial/full receipts, either
working-time choice, canonical request token validation, unknown-token rejection,
changed schema, routing changes, leaver stop, staff-recorded RTW/DBS receipt,
72-hour planning, four-email cap, restart persistence, uncertain-delivery hold,
complete pagination and exact jobs/HR sender payloads with no CC/BCC.

Prior independent live tests verified the approved Jotform schemas and Monday
read connection, isolated synthetic receipt discovery/stopping and persisted
ledger stopping. The 12 October screenshot confirms that the agreed non-sensitive
test reached andy@securecareservices.co.uk from hr@securecareservices.co.uk with a
clickable link. The automated mailbox metadata verifier remained false; the
screenshot is independent evidence, not an automatic verifier result. The isolated
test form's native Jotform notifications were independently verified off.

Deployment-specific background and board-write proof must be checked after this
change goes live. No real staff are enrolled merely by deploying it.
