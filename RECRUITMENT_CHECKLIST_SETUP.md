# Recruitment administrative follow-up draft

This draft is not imported by the application, installed in the production image,
or connected to a sender. The live reference worker continues independently.

The draft adds a persistent five-item checklist, a combined reminder planner,
clickable HTML and plain-text email templates, and coverage/completeness helpers.
Only canonical linked receipts or explicitly recorded staff receipt events stop
an item. Receipt means staff review is needed, never approval or clearance.

## Verified existing sources (12 October 2026)

Monday board 424237712:

| Requirement | Existing board field | Existing form | Stop candidate chase |
| --- | --- | --- | --- |
| Ten-year employment history | status7 | HR01f 211923425771051 | Linked completed submission or staff receipt |
| Medical disclosure | status72; separate approval status_mkmvm8rd | HR01e 90715470243352 | Linked form receipt; approval remains separate |
| Right to work | status583; expiry date08 | Staff check and secure document route | Evidence received or checking appointment arranged; staff verifies |
| 48-hour choice | status247 | PM13 82551787411460 | Either IN or OUT, or linked recorded choice; cancellation to staff |
| DBS application information | status79/status3; consent update_service_consent6 | HR01b 92583319416361 | Required details received; identity and application checks to staff |

The right-to-work form QQ02 261643387579371 is an organisational audit,
not an applicant evidence form. Do not send it to candidates.
HR01h 232493999918378 records interview/document checks by staff.
PR04B 211753252627050 records Update Service consent; that consent is distinct
from authorising a new DBS application and must not be substituted.

## Rules

- Exact verified candidate/source links are required. Names alone are insufficient.
  Do not enrol the historic backlog, change old default consent, or infer a new request.
- Staff explicitly requests the relevant checklist items. Medical disclosure is
  requested only at the appropriate approved recruitment stage. No health answers
  or diagnoses are read by this checklist, scored, copied to Monday, or emailed.
- History covers employment, education and explained non-working periods equally.
  The date helper flags uncovered dates, not the acceptability of a gap. Unknown dates,
  ambiguous widgets, young applicants and policy exceptions need staff review.
  Form submission is receipt; missing dates/explanations require a new specific
  staff follow-up, not silent reopening of the campaign.
- DBS completeness requires presence flags for name, DOB, declaration of previous
  names, current address, five-year address history, identity documents and new
  application authorisation. No raw document data is stored here. Staff selects
  the applicable check/identity route, verifies documents, and submits the application.
  A received upload is not proof of valid identity or eligibility.
- Right-to-work evidence/check appointments are arranged through an approved secure
  route. Do not demand a share code from everyone. Staff conducts and records the
  correct check, restrictions and follow-up date; no automatic eligibility decision.
  Expiry reminders for existing employees are a separate staff workflow.
- Either working-time choice stops the candidate reminder. No default opt-out,
  repeated pressure, or negative employment inference. Cancellation goes to staff
  for updating working-time records, not automatic re-enrolment.
- One combined email from jobs@securecareservices.co.uk contains only outstanding
  items and clickable secure form links. No diagnoses, document numbers or sensitive
  information is requested in an email reply.
- Initial request plus at most three reminders, 72 hours apart. Maximum applies to
  the whole campaign to avoid five simultaneous chases. Exhaustion goes to staff.
- Re-read canonical receipts, candidate address, stage and active recruitment state
  immediately before sending. Stop for withdrawal, rejection, employment transition,
  uncertain linkage or conflicting evidence. Restart after sending never retries an
  uncertain delivery. `send_pending` on restart must be reviewed rather than resent.
- Existing Monday `is_done` flags are unsafe as evidence: partial employment history,
  DBS type 'None' and DBS consent 'No' are marked done in the current schema.
  Medical receipt and Medical Disclosure Approval are separate fields.

## Activation work still required

1. Establish verified per-form candidate/request IDs and schema-tested receipt
   adapters. Do not match the medical/history forms by name alone or expose their
   answer bodies in logs. Store IDs and presence/receipt flags only.
2. Review existing native automations before enabling a new candidate sender;
   disable only verified duplicate candidate chases with an agreed cutover baseline.
3. Obtain explicit approval for new persistent Monday access and extending the
   background Jotform read scope beyond the two reference forms already approved.
   The existing connector can inspect metadata; that does not create background access.
4. Wire exact-match source adapters and the existing jobs sender, protected review
   routes, health reporting and staff queue. Respect the existing reference consent
   gate; an employment-history submission must never enrol referees by itself.
5. Test with a fictitious applicant and the previously authorised Andy test address
   only after the expanded test scope is confirmed. Verify partial receipt removes
   just that item, either 48-hour choice stops, all receipt stops, unknown delivery
   holds, and state survives a restart before a controlled live rollout.

Official guidance used for the draft:

- https://www.gov.uk/check-job-applicant-right-to-work
- https://www.gov.uk/maximum-weekly-working-hours/weekly-maximum-working-hours-and-opting-out
- https://www.gov.uk/government/publications/dbs-application-forms-guide-for-applicants/dbs-application-form-guide-for-applicants

## Continuation preflight, 12 October 2026

All 5,841 board records were checked through three complete paginated reads.
The new verified HR01b source-ID column is blank in every record. This proves
the new linkage has not been populated, not that no submissions exist.
Do not guess candidate links or bulk enrol the backlog.

The board has three workflows (two active, one inactive duplicate) and 98 other
automation records. Existing right-to-work expiry reminders cover three months,
one month, one week and one day, conditioned on Employed with ESCS. These belong
to the post-employment workflow and must not be duplicated by recruitment chases.
Existing medical annual renewal is also conditioned on employed staff and sends
through an Outlook connection. Its sender must be verified independently before
claiming compliance with the jobs/HR mailbox split.

Two existing reminder copy errors require correction in their original editor:
238609118 triggers three months before expiry but its body says one month;
238609412 triggers one week before expiry but its body says tomorrow.
They cannot be edited through the current connector. No automated rule was disabled
or changed during this preflight. Existing DBS review reminders also remain intact.

`recruitment_receipts.py` adds canonical ID/form/status and signature-schema checks
for explicitly verified history, medical and working-time receipt links. It returns
identifiers only, accepts any valid working-time choice without scoring it, rejects
duplicate/unknown choices and conflicting receipt ownership, and validates a whole
provider batch before applying any receipts. Actual live question IDs/types/options
still need verification; metadata alone is insufficient. The adapter has no network
access, stored credentials or sender. Right-to-work and DBS receipt adapters remain
staff evidence/completeness paths, not blanket "form received = cleared" rules.

Validation: `python -m unittest -q test_recruitment_checklist test_recruitment_receipts`.
