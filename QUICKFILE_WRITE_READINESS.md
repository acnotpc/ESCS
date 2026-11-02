# QuickFile next-stage readiness — 1 November 2026

## Implemented in this change

`POST /quickfile/previews/invoice` requires the existing `X-Connector-Key`.
It validates a proposed GBP invoice, calculates per-line net/VAT/gross values
using Decimal and half-up penny rounding, and returns a review payload and
content fingerprint. It makes no upstream request and creates no invoice.
The fingerprint identifies content; it is not an approval token or a duplicate
prevention mechanism. Exact decimal strings in the preview are not yet a
wire-ready API request (the API documents numeric JSON amounts).

`proposal_sha256` remains the payload-only identifier. `review_sha256` hashes
the entire returned review envelope except itself: invoice payload, job,
evidence references, calculations, totals, verification flags and review
requirements. Canonical JSON uses sorted keys, compact separators and UTF-8.
`review_fingerprint_version=escs-quickfile-review-v1` is included in that hash.
Changing any source reference changes the review fingerprint even when the
financial proposal is identical. `approval_supported=false`: no approval is
accepted, stored or carried forward by this preview endpoint.

Before approval/write functionality is added, persist an immutable review
snapshot server-side, including source record revisions or captured content
digests and the exact attachment bytes' digest. A URL alone cannot detect a
document changing at the same URL. Bind an authenticated, independent reviewer's
decision to that snapshot, account, operation and fingerprint, with expiry and
atomic consumption. Recompute and compare against the stored approval at the
write boundary; reject mismatches and require a new review. Never accept a
client-supplied fingerprint or the payload-only hash as authority. A plain hash
detects content differences; it provides neither identity nor authorisation.

Required source references cover booking form, Monday actual work, FMS hours,
RAC mileage and rate agreement. References are operator-supplied assertions;
the endpoint does not fetch or verify those sources. Duplicate and attachment
verification are explicitly outstanding. VAT must be explicitly supplied per
line; no customer, rate, VAT, bank or payment-term defaults are inferred.
Eight-hour minimum wording is rejected in notes/payment terms; this is only a
wording check, not validation of contractual chargeable hours.

## Verified current API operations

Source: https://api-beta.quickfile.co.uk/api-docs/v2 (Swagger 2 schema retrieved
and read on 1 November 2026); UI: https://api-beta.quickfile.co.uk/api-docs/.

| Capability | Documented operation | Readiness gap |
| --- | --- | --- |
| Create | POST /invoices, InvoicesCreateRequest | No explicit draft/status property. Confirm draft and no-notification behaviour in an authorised sandbox or with QuickFile before a production pilot. |
| Amend | PUT /invoices/{id}, InvoicesUpdateRequest | Required type/client_id/currency; determine field preservation/replacement behaviour in sandbox. Limit initial amendments to drafts, compare original hash, and re-read after save. |
| Send | POST /invoices/{id}/send | Resolve and confirm client_contact_id, final content and actual attachment; use by_email=true, by_snail_mail=false only after second-person approval. |
| Record receipt | POST /client_payments | Confirm actual bank receipt and allocation; type=payment, invoice_id/client_id/date/amount/currency/nominal_code. Keep apply_credit=false and send_confirmation=false unless explicitly approved. This records accounting data, not a bank transfer. |
| Attach document | POST /documents/sales | Multipart file plus invoice_id and optional notes. Verify the exact source file and target invoice before upload. |
| Verify existing attachment | No documented GET document list/download operation found in current schema | Retain authenticated browser inspection/download and compare actual attached contents with original Jotform/Monday evidence. An upload response or invoice note is not proof of correct attachment. |

QuickFile staff also identify POST /documents/sales for invoice/estimate files:
https://community.quickfile.co.uk/t/rest-api-file-attachmente/66259.

## Before enabling writes

1. Inspect current token permissions without exposing its value. Read success
   does not prove write entitlement. Confirm expiry/rotation; the existing
   setup document records 30 November 2026 for the pilot token.
2. Confirm draft creation/no email semantics and update behaviour in a
   separately authorised test account, or obtain explicit provider guidance.
   Do not test writes on real invoices to discover behaviour.
3. Add a separately protected write boundary and disabled-by-default controls
   per operation. Keep read-only MCP tools read-only. Exposing broader account
   access requires explicit approval of the concrete permission change.
4. Bind approval to the exact customer, job, source evidence, amounts, terms
   and attachment. A second person checks the draft before sending; approval
   expires if relevant content changes.
5. Search paginated invoice and credit-note records for the job before create;
   reserve the job/operation atomically in a durable audit store. Record pending,
   completed and uncertain outcomes. On timeout, reconcile before retrying;
   no upstream idempotency guarantee was found in the published schema.
6. Read back every successful write and compare target identity and values.
   Preserve issue-date versus conveyance-date distinctions. Handle nullable
   notes, search-only due_date and REFUND/credit note status differences.
7. For receipts, reconcile existing bank transactions/tags and allocations to
   avoid recording the same money twice. For amendments, define treatment of
   already issued or paid invoices before expanding beyond drafts.

No live write, permission expansion, upload, send or payment allocation has
been tested or enabled by this change. The preview is a preparation step,
not an automatic invoice-verification or approval system.

## Validation

`python -m unittest test_quickfile_preview test_quickfile_read -v`

Tests cover protected access, exact arithmetic, content fingerprint changes,
required source references, rejection of invalid monetary values, prohibited
fields/wording, and retained read-only behaviour. No production writes occur.
Additional regression cases cover each of the five changed evidence references,
invoice changes, deterministic key ordering, exact review-envelope hashing and
rejection of supplied approval/verification fields. All 11 targeted tests pass.

