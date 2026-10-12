# ESCS per-referee chasing service — controlled rollout

The user selected an ESCS reminder service so receipt by email can stop its own
reminders without an unsupported Jotform task-completion API. This draft replaces
both initial reference invitations and reminders after a controlled migration;
it retains the current new-starter and reference forms. It is wired into app.py and the Docker image with a cancellable lifespan worker.
All activation flags default to disabled; live migration is a separate configuration
step after protected-route and receipt validation.

## Implemented behavior

- Canonical Jotform source data only, pinned to new-starter form 92583319416361.
  Only the exact answer Yes on the existing both-referees consent question
  permits enrollment. Missing/ambiguous consent fails closed. No/blank is skipped.
- Two distinct nominated email addresses, one random request ID per referee.
  Duplicate submission processing retains those IDs and cannot resend an initial
  invitation. Changed nominations require review rather than silent replacement.
- Enrollment is restricted to IDs after a verified fixed activation baseline.
  Historic preselected Yes answers are excluded. Baselines cannot reset on restart.
- Source consent and nominees are checked again before every send. Withdrawal or
  changed nominees stop that pending request. The sender is fixed to jobs@; each
  message goes to one nominee and contains the candidate name and reference ID.
- Initial invitation, then at most three reminders separated by 72 elapsed hours
  (three calendar days). No catch-up burst after an outage. Receipt processing
  precedes sending. Separate request states keep the other referee's chasing active.
- A matching canonical reference submission stops that request. An email from
  the nominated sender to jobs@, with its request ID and an explicit attached
  completed-reference statement, stops chasing as received, unverified.
- Out-of-office responses are ignored. Ordinary acknowledgements, future promises,
  negative statements and uncertain replies pause chasing for staff review.
  Quoted/forwarded attachment claims are excluded. Names alone never match receipts.
- Failed/incomplete receipt scans prevent sends. A send intent is saved before the
  network request. Timeout or restart during sending leaves send_unknown for review,
  never an automatic resend. A subsequent matching receipt can resolve that state.
- Receipt and professional verification remain separate. No eligibility, ranking,
  hiring, rejection, DBS or right-to-work decision is automated.

The email rule records receipt based on explicit matching reply evidence; it does
not inspect the attachment's content or authenticate the referee professionally.
Ambiguous, alternate-sender, untracked or incomplete references still require staff.
Polling can race with a reply arriving after the scan: one in-flight reminder may
still arrive. The service stops future reminders once receipt is observed.

## Files and adapters

- reference_chasing.py: SQLite request ledger, consent gate, receipt evidence parser,
  cadence and failure-safe sending engine.
- reference_chasing_adapters.py: pinned EU Jotform reads, scoped Bitrix send adapter,
  canonical form/email receipt scans and source enrollment.
- reference_chasing_service.py: opt-in five-minute worker and protected operator
  routes. All routes require the existing X-Connector-Key dependency. There are
  no public/conversational mail or send tools.
- test_reference_chasing.py: synthetic/local tests, no real applicant or referee
  submissions and no live emails.

## Remaining setup and verification before migration

1. Completed 12 October 2026: owner approved a dedicated **read-only Jotform API key**, created with Read Access and stored in
   Render as JOTFORM_RECRUITMENT_READ_API_KEY. Jotform may grant account-wide form
   read access; this code queries only the two pinned recruitment forms. A source
   submission is fetched only from pinned-form discovery/ledger IDs and checked
   against the expected form. Never expose keys in chat, code or URLs; APIKEY is
   sent in an HTTP header. Do not broaden the key to Full Access.
2. Completed 12 October 2026: added an optional hidden short-text field titled exactly **ESCS reference request
   ID** to HR01c. Leave it optional so old/native links keep working. The new service
   prefills it. Canonical questions API verified candidate name `nameOf` (short text),
   referee email `emailAddress12` (email), and request ID `escsReference` (short text,
   hidden Yes, required No). Missing or unsupported schema blocks every send.
3. In an isolated test, verify actual API paging/response schema, Bitrix sender
   success, unique form link/ID retention, both referee paths, one-person completion,
   email receipt, ambiguous response, consent No/blank/withdrawal, timeout/restart
   and operator confirmation. Use approved controlled recipients, not candidates.
4. Completed: wired the modules into Dockerfile/app.py after validation. Reuse the
   existing one-instance /var/data persistent disk. Launch service.loop in lifespan
   with cancellation on shutdown and build_chasing_router with require_connector_key.
5. Capture the highest current source and reference submission IDs through a verified
   complete API read at the migration point. Save both as fixed baselines. Check that
   there are no in-flight native tasks to duplicate before changing the native flow.
6. Disable the existing native reference workflow on the new-starter form, verified
   through its attached workflow menu. This is a replacement, not a second chasing
   system. Keep other form acknowledgements enabled. No existing backlog is enrolled.
7. Enable the worker initially with writes false. Confirm the persisted ledger and
   due previews. Set the native-disabled attestation only after live read-back.
   Then activate writes within the user's approved recipient/consent scope.

## Environment (all default disabled)

- ESCS_REFERENCE_CHASING_ENABLED=false
- ESCS_REFERENCE_CHASING_WRITE_ENABLED=false
- ESCS_NATIVE_REFERENCE_WORKFLOW_DISABLED=false
- ESCS_REFERENCE_CHASING_DB=/var/data/recruitment-references.sqlite
- ESCS_REFERENCE_SOURCE_BASELINE=<verified highest pre-activation source ID>
- ESCS_REFERENCE_FORM_BASELINE=<verified highest pre-activation reference ID>
- JOTFORM_RECRUITMENT_READ_API_KEY=<dedicated read-only secret>

The existing BITRIX_RECRUITMENT_MAIL_REST_URL and mailbox ID are reused. Live
sending cannot start while the native-disabled flag is false. A deployment or
write flag alone never grants consent for a nominee. No HTTP request body can
substitute for canonical Jotform consent/source verification.

## Protected operator actions

- GET /reference-chasing/status: counts/state/error, no applicant data.
- GET /reference-chasing/requests: staff-only names, nominees and request states.
- POST /reference-chasing/run: same source/receipt gates as the worker.
- POST /reference-chasing/requests/{request_id}/confirm-email/{message_id}: staff
  attest a matched email is the completed reference. Reads canonical jobs@ mail,
  verifies sender/request matching and excludes automatic replies before recording
  received. This never records professional validity or an employment decision.

## Validation

`python -m unittest -v test_reference_chasing test_recruitment_mail`

48 tests passed with synthetic sources, MockTransport and TestClient; Python
compilation passed. Live read-only validation on 12 October 2026 passed for both
pinned forms: questions API HTTP/API 200, two pages of 100 submissions with
descending, unique IDs and correct form identity. A fictitious, unsubmitted public
reference link correctly prefilled candidate, referee email and hidden request ID.
A later owner-authorized fictitious test was sent from jobs@ to andy@ on 12 October;
Bitrix confirmed send success and the owner confirmed delivery. The incoming reply
was observed after opening/synchronizing the jobs mailbox. The first test form was
submitted, but the tracking ID was blank and candidate did not match the fictitious
request. It was correctly not accepted as a matched receipt. The raw-URL email
format was replaced by HTML with an explicit button and copyable clickable fallback;
48 tests now include link target and candidate escaping checks. The corrected test was submitted with the exact request ID, fictitious candidate
and nominated referee. Actual ReceiptScan and Engine processed that live canonical
receipt in an isolated ledger: the completed referee was excluded from due reminders,
the second unsent synthetic referee stayed due, and received state survived restart.
No email was sent by that verification. Form receipt stopping passed; a live completed-email attachment test and worker
migration remain pending. The
protected jobs@ reader was previously tested live separately. Native reference
workflow settings are unchanged. Writes remain disabled.

Observed highest IDs at validation (do not use as activation baselines without a
fresh migration-time check): source 6672949174609332645; reference
6674192006541160339. No applicant data or API credentials are stored in this document.

Official interfaces used:

- https://api.jotform.com/docs/
- https://apidocs.bitrix24.com/api-reference/mail/message/mail-message-send.html
- https://apidocs.bitrix24.com/api-reference/mail/message/mail-message-list.html
- https://apidocs.bitrix24.com/api-reference/mail/message/mail-message-get.html
