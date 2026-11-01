# Protected MMVA delegate attendance

Adds `/attendance` to the existing ESCS Render web service. No new paid service is needed. Existing routes are unchanged. The feature defaults to unavailable and does not open registration merely by deploying this code.

## Standard trainer QR and individual login

The permanent trainer QR encodes `https://escs.onrender.com/attendance/trainer`. It never contains a course, password or setup token. `/attendance/trainer/qr` serves its SVG. Trainers scan it, sign in with their own email/password, select a Monday course (optionally filtering date, venue or client), confirm its details, and display/print the separate course-specific delegate QR. No course is auto-selected.

Trainer accounts persist in the existing attendance SQLite database. Passwords use salted PBKDF2-HMAC-SHA256 with 600,000 iterations; no plaintext password is stored. Password setup requires an administrator-issued, random, hashed invitation token that expires in 24 hours and can be consumed only once. Setup tokens are placed in the URL fragment (not request logs), removed from the address bar immediately, and posted in JSON only when the user creates their password. Users choose passwords of at least 15 characters. No credentials or invitation emails are sent automatically.

Administrator endpoints use the existing `X-Connector-Key` header (never give this key to trainers):

- `POST /attendance/admin/trainers`: `{ "email": "trainer@example.com", "name": "Trainer name" }`. Returns a one-time `setup_path` and expiry. Deliver the setup link privately; do not log it, commit it, or place it in a shared QR. A duplicate account returns 409. An explicit `reset: true` issues a replacement link and revokes existing sessions.
- `POST /attendance/admin/trainers/disable`: `{ "email": "trainer@example.com" }`. Revokes only that account's login and invitations, leaving other trainers unaffected.

There is no public self-registration or automatic email recovery. An administrator issues a replacement private setup link when needed. New credential entry should be completed by the user in the setup page. Trainer sessions are signed HttpOnly/Secure/SameSite=Strict cookies limited to `/attendance/trainer` and expire after eight hours. Every protected request checks the account's enabled flag and version in SQLite; password reset or account disablement invalidates existing sessions. Failed attempts are throttled persistently by hashed IP (30) and email (10) in a five-minute window, including across server restarts. Unknown users receive the same error and password-derivation work as incorrect passwords.

The previous `MMVA_ATTENDANCE_TRAINER_ACCESS_CODE` setting is no longer used. No new Render secret is required for individual trainer accounts; the existing stable attendance signing secret is reused with a distinct session salt. Existing shared-code cookies cannot authenticate to the new account routes.

After login, choose a date range (default today through the next 60 days, maximum 90 days), optionally filter venue or search client/course/ID, explicitly select and confirm one course, then generate its QR. `address__1` from the booking board provides the venue/booking address; missing values display 'Venue not recorded'. Separate IDs remain separate even at the same venue/date. The list and preview are private and not cached. The public known-ID preview does not gain venue data, booking contacts or a catalogue.

The read-only catalogue uses Monday's nested `boards.items_page` and `next_items_page`, 500 rows per page, with a 10-page safety limit that fails instead of returning a silently incomplete list. Only active, valid-dated, attendance-open courses intersecting the date range are shown. Registration and attendance still recheck the exact booking's current status and today's date. Delegate QR links do not require trainer login.

## Trainer setup and course-specific QR signs

Open `/attendance/trainer`. Sign in, select the course and verify its title, dates, venue and Course ID. Known Course ID entry is also retained as a fallback. The page generates a local SVG QR code, a printable sign and a copyable link to `/attendance?course=COURSE_ID`. The ID is prefilled and hidden on the delegate page. The actual registration and every subsequent entry still validate the code, booking status and date on the server; editing a link cannot bypass these rules.

Use the same course-specific QR across all attendance points and booked course dates. A shared-device user can choose another delegate without losing the selected course. When a delegate scans a different course QR, an existing session for another course is not displayed as the selected course. The generic QR remains available but requires code entry.

The trainer screen performs exact ID lookup, not a public list of bookings. Public course-info returns only the matching ID, course/client title and start/end dates, never the booker's contact details or a delegate roster. It is rate-limited. This screen does not grant trainer administration access, alter bookings, or remove the server's active-date restrictions.

## Monday.com Course ID connection (live configuration)

Set `MMVA_ATTENDANCE_COURSE_SOURCE=monday` and `MMVA_ATTENDANCE_JOTFORM_COURSE_FIELD=14` after deploying this version. These settings supersede the random course-code creation workflow below. The numeric Course ID is the item ID on booking board `4727626523`, also used as its Course ID Tag; it is not the tag's internal numeric tag ID.

The gateway reads only the exact entered item. It checks the item is active on that board, has Booked / Confirmed with Client / Trainer Allocated / Course Ongoing status, and today in Europe/London falls within its start/end dates (timeline is a fallback if date columns are empty). It refuses missing/invalid dates, unknown status, cancelled/completed courses, wrong-board items and API failures. There is no public roster or course catalogue.

Use `MMVA_ATTENDANCE_MONDAY_API_TOKEN` if supplied; otherwise the existing `MONDAY_RECRUITMENT_API_TOKEN` is used for this read-only booking lookup. Access to the target MMVA booking was verified before enabling it. There are no Monday mutations or automatic changes to bookings, attendee outcomes, certificates or statuses.

The canonical ID is shown to the delegate, stored in Jotform's explicit **Course Number** question (14), and included in the gateway audit note with the booking link. This provides a shared identifier; it does not itself create a new Monday attendance-log item or configure integrations on other forms.

An internal SQLite course-day key combines the unchanged Monday ID and date so each day starts at morning sign-in. This internal key is never used as the external Course Number. Existing SQLite tables are migrated additively. No attendance is deleted. Course status is rechecked on each status/record request, so a booking cancelled after registration cannot accept another attendance entry. No cached booking is used when Monday is unavailable.

`POST /attendance/admin/check-course` with `{ "code": "13436327815" }` provides a protected read-only check including start/end dates and whether it is scheduled today. It can inspect future courses without opening them for attendance. Random-code creation through `/attendance/admin/courses` is disabled in Monday mode.

The same QR URL remains valid. The field now accepts numeric Monday Course IDs up to 20 digits. For duplicate booking records, the exact supplied ID is used; the gateway does not silently merge them.

## Legacy/manual delegate flow (used only when Monday mode is off)

The trainer creates one course-day record through the protected API and supplies the returned eight-character course code. The QR sign points to `/attendance`. The delegate enters that code, name, email and organisation. Only the next action is shown, and the server accepts exactly the sequence morning in → lunch out → afternoon in → final out. A signed, expiring token binds the next action to a delegate. A SQLite transaction and unique constraint stop concurrent duplicates and replay. Email matching is case-insensitive and whitespace-normalised. Course date and title come from the trainer's course record, not delegate inputs. London date is authoritative. A new course-day code resets the sequence.

This records self-declared attendance, not verified identity or physical location. Someone knowing another delegate's email and the course code could impersonate them. Trainer supervision remains necessary. The 60-second interval stops rapid accidental progression; it does not define actual lunch times. Early departure, missed scans and corrections require trainer review; delegates cannot bypass stages.

## Configuration in Render

Use the existing ESCS service in **My Workspace**, with its existing `/var/data` persistent disk and one instance. Do not use an ephemeral filesystem or scale this SQLite service to multiple instances. Back up the attendance database using SQLite's backup API, alongside existing disk backup practices.

Set these in the Render environment, never source control or chat:

- `MMVA_ATTENDANCE_SECRET`: securely generate a stable random secret of at least 32 characters. Preserve it across restarts; rotating it invalidates sessions and changes matching of existing emails.
- `MMVA_ATTENDANCE_JOTFORM_API_KEY`: a Jotform key permitted to create submissions in form `263045888335062`. Do not reuse the recruitment read-only key. Use the least-privileged dedicated Jotform account/key available.
- `MMVA_ATTENDANCE_DB=/var/data/mmva-attendance.sqlite3`
- `MMVA_ATTENDANCE_ENABLED=true`
- `MMVA_ATTENDANCE_DESTINATION_VERIFIED=true` **only after** the checks below pass.

Both flags must equal the lowercase string `true`. Missing secret, key or persistent parent directory causes public API calls to return 503; the page explains that registration is not open.

## Activation checklist

1. Deploy and verify `/health` and `/attendance` without opening registration.
2. Confirm the Jotform destination question mapping against the live questions: 3 full name, 4 email, 5 organisation, 6 course title, 7 course date, 8 numbered attendance action, 9 trainer, 10 note, 11 confirmation. API encoding follows Jotform's documented `submission[3_first]` / `submission[3_last]` convention. Validate all returned answers, including date and confirmation, in an explicitly identified test submission before live use.
3. Remove the direct public submission route to the Jotform form (e.g. disable public availability). Verify that authorised API submissions still succeed while public submissions cannot be made. If that combination is not supported by the account, use a private destination accessible only by the service and adjust the mapping; do not claim bypass protection while the original form remains publicly writable. Do not delete prior attendance records.
4. Configure the stable secret and write key securely in Render. Use a test course with fictional delegate details, then enable the verification flag only after the destination checks succeed. Temporarily enabling flags for a controlled test requires keeping the gateway URL undistributed until completed.
5. Verify all four steps in Jotform, invalid stage rejection, duplicate rejection and persistence across restart. Reconcile or label test records before live use. No automated test creates real submissions.
6. Replace the old printed QR sign. The old QR points directly to Jotform and does not gain server protection automatically. New target: `https://escs.onrender.com/attendance` after deployment verification. Print and test that sign only after activation.

## Trainer administration

Use the existing protected connector header `X-Connector-Key`; never include it in a URL.

- `POST /attendance/admin/courses`: JSON `{ "title": "Course title", "day": "YYYY-MM-DD", "trainer": "Trainer name" }`. Returns a random course code. Create a separate course-day record for each date. Do not create another code for the same course-day to work around an attendance problem.
- `GET /attendance/admin/status`: readiness plus delivery-state counts, without attendee names.

A shared-device user must choose **Use another delegate's details** before handing over the phone. Delegates should use the same email throughout the day. Sessions are HttpOnly, Secure, SameSite=Strict cookies and expire after 18 hours. A delegate who clears cookies can re-enter their details with the same code to resume.

## Durable recording and Jotform delivery

The SQLite event and server timestamp commit before sending a copy to Jotform. The note includes the unique gateway event ID and the actual Europe/London timestamp; Jotform's own creation time can be later. New records start `pending`, are claimed as `sending`, and become `sent` only on a confirmed API response with a submission ID. Any failure or ambiguous response becomes `uncertain`. A process interruption can leave a `pending` or `sending` row. The UI tells delegates their entry is saved; status warns if Jotform confirmation is outstanding.

There is deliberately no blind automatic retry of uncertain requests because Jotform does not supply an idempotency guarantee here. An administrator must reconcile pending/sending/uncertain rows by gateway event ID against Jotform and then arrange a controlled recovery. The local ledger remains the sequence source of truth. There is no automatic import of earlier direct Jotform submissions; start this gateway on a fresh course day. This version has no correction UI or scheduled sync worker.

## Tests

`python -m unittest test_mmva_attendance test_mmva_monday_courses test_mmva_trainer_picker test_mmva_trainer_auth -v`

Tests use temporary SQLite files and mocked Jotform HTTP responses. They cover ordered progression, rejected skips/replays, case-insensitive identity, course-day rollover, cooldown, concurrency, restart persistence, disabled configuration, unauthorised admin access, signed-token tampering, required confirmation, request format and safe handling of ambiguous delivery. Live API permissions, field mapping, private destination behaviour and deployment remain activation checks.

## Course-linked form QR codes

After signing in and confirming a course, the trainer can select attendance, learner registration (T04A), pre-course health declaration (T04B), post-training fitness (T04D), PI knowledge assessment (T04F), venue risk assessment (T01B), accident/incident report (HS23), or general course evaluation.

The seven Jotform links share the exact canonical Monday Course Number. Company text fields are prefilled from the booking; evaluation also receives the course title and keeps the learner name optional. Venue risk assessment is labelled for trainer use; accident reporting is labelled for use when needed. No personal details or responses are placed in QR codes.

Course-form QR destinations use a separately signed, purpose-specific token, expiring after 90 days. The gateway verifies the signature and rechecks the exact active Monday booking on opening, then redirects only to a fixed allowlist of Jotform IDs with URL-encoded prefills. It does not apply attendance's today-only restriction to pre-course forms or later reports. Trainers can regenerate links by selecting the course again.

These existing Jotform forms use prefilled fields: this does not add server-side enforcement of their submitted Course Number, configure new Monday writeback, or make them subject to the attendance sequence. Their existing integrations, validation, assessment scoring and privacy behaviour continue independently. The attendance gateway remains the only sequence-enforced route.

## Trainer password recovery

The sign-in screen links to `/attendance/trainer/forgot`. Requests always return the same generic response, including for unknown, disabled and rate-limited accounts. Email generation and sender validation run after the response. Only an existing enabled registry address can receive a reset email; there is no public signup or arbitrary mail endpoint.

Reset tokens are random, stored only as SHA-256 hashes in additive SQLite columns, delivered in URL fragments and removed from the browser address as soon as the page loads. They expire in 30 minutes, work once and are separate from invitation tokens. A completed password change invalidates all existing trainer sessions and outstanding setup/reset links; requesting a reset leaves the existing password and sessions working. Persistent per-address, per-IP and global request limits restrict abuse. Password rules and hashing are unchanged.

The fixed-purpose recovery sender uses the existing user-1 Bitrix mail REST connection (`BITRIX_RECRUITMENT_MAIL_REST_URL`). Before sending, it verifies the exact `info@mmva.co.uk` mailbox and permitted sender identity. It sends only the fixed MMVA reset message to the registry address, never arbitrary request content. Provider errors are suppressed from the public response and produce a generic operational warning without credentials, addresses or reset links. Uncertain sends are not automatically retried. A trainer may request a new link within the configured limits.

## Hidden course bookings

`MMVA_ATTENDANCE_EXCLUDED_COURSES` is a comma-separated list of exact canonical Monday Course IDs. Excluded bookings are omitted from the picker and rejected by exact course, QR launch and attendance lookups. This is portal configuration only: no Monday bookings or historical evidence are deleted. Andrew confirmed hiding `13060644417` (Corran Support, Smokey Farm, Pershore) after the supplied ID `1306064417` was not found.
