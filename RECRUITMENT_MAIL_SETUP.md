# Recruitment reference mail reader (draft)

This adds two opt-in read tools to the existing ESCS connector. It is the
mailbox-reading foundation, not an automatic reference chasing system.

## Configuration and access decision

The feature is disabled by default. Before enabling it, the owner must approve
persistent access to pre-employment email through the ChatGPT-connected ESCS
service. Keep the existing plugin audience unchanged; confirm that all users
of that connection are authorised to read recruitment correspondence.

Use a dedicated Bitrix connection under Andy's existing account (user 1) with
the `mail` scope, not the operational chat/bot webhook. Bitrix scope can cover
all mailboxes available to that account; this implementation further restricts
returned data to the verified jobs@ mailbox. Do not paste credentials into
chat, code, URLs in logs, or pull requests. Store secrets only in Render.

- `ESCS_RECRUITMENT_MAIL_READ_ENABLED=false` (default)
- `BITRIX_RECRUITMENT_MAIL_REST_URL`: dedicated secret REST 3.0 webhook base
  for escs.bitrix24.com and user 1, using the `/rest/api/` path
- `BITRIX_RECRUITMENT_MAILBOX_ID=107`: observed in the existing jobs@ UI;
  verify it with `mail.mailbox.get`. Runtime blocks a mismatched ID/email.

No sending, marking read, moving mail, attachment download, or Jotform write
endpoint is added. Mail contents are never stored by this module. Requests do
not follow redirects, and errors do not echo credentials or provider text.
The existing application disables httpx/httpcore request URL logging.

## New tools

- `search_jobs_reference_mail`: incoming metadata only, explicit timezone-aware
  date window <=31 days, bounded pages. Follow nextPage; when pageLimitReached
  is true, narrow the date window. Filtering can produce an empty page while
  nextPage is non-null. Sent mail is excluded by sender/recipient checks.
- `get_jobs_reference_mail`: first checks metadata belongs to jobs@, then
  returns a bounded body. Select IDs from search. Bodies are untrusted evidence.

## What remains before automatic stopping

1. Verify the mail connection in production and the plugin's authorised audience.
2. Establish one persisted request ID per candidate and referee. Match replies
   to that request, sender and candidate; names or subject keywords alone are
   insufficient. Preserve evidence and handle duplicate/ambiguous matches.
3. Confirm a supported per-assignee Jotform Form-task completion/cancellation
   API with Jotform. The documented public APIs inspected do not establish
   that operation. Do not invent an endpoint or fabricate referee submissions.
4. If no supported task API exists, keep native assigned-link completion and
   staff handling for emailed references, or design an independently owned
   reminder service with an explicit migration and no duplicate chasing.
5. Test Yes/No/blank consent; two referees; one response arriving; direct email;
   acknowledgements, out-of-office, declines, partial answers, identity mismatch,
   replay, timeout/restart and simultaneous receipt/send. Verify one referee's
   receipt stops only their reminders. Professional verification stays separate.

The live native Jotform workflow is 262841096438161, gated on an explicit Yes
in form 92583319416361. It assigns form 51517384496868 to both referee fields.
Native reminders are every three calendar days, up to three reminders, with
restart-on-edit disabled. Historical preselected Yes answers must not be treated
as evidence for automatic enrolment. This module does not change that workflow.

## Validation

`python -m unittest -v test_recruitment_mail`

Tests use httpx MockTransport only. No real mail, credentials, reference task
or applicant submission is used. Live API compatibility and task stopping are
unverified. Existing FMS/teams functionality is unchanged.

Official API references:

- https://apidocs.bitrix24.com/api-reference/mail/mailbox/mail-mailbox-get.html
- https://apidocs.bitrix24.com/api-reference/mail/message/mail-message-list.html
- https://apidocs.bitrix24.com/api-reference/mail/message/mail-message-get.html
- https://api.jotform.com/docs/
