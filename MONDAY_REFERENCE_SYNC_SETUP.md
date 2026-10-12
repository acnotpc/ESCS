# Recruitment-board reference summary — draft connection

The live reference chasing worker remains enabled. This extension is saved for
review but must not be activated until the owner approves storing a Monday API
connection in Render. A personal token can grant account-wide access; code pins
all requests to board 424237712 and writes only its new summary column.

## Fields already added and read back on the live board

- Reference automation source ID: text_mm82vbx9 (text).
- Reference automation update: long_text_mm8220ks (long text).

The source ID must be the exact, verified HR01b new-starter submission ID for that
candidate. A missing, duplicate or incorrect source link is held for staff review;
the integration does not guess by name, create candidate items, or populate source
IDs on behalf of staff. Intake mapping from the existing application integration
is a separate step and must be verified before enabling routine writes.

## Behavior

- Retrieves only the fixed board schema and items matching a known source ID.
- Confirms pinned new-starter form identity and canonical candidate name, then
  checks the board item ID, source link, full name and active recruitment status.
  Name alone never links records. Both full item names and first-name plus the
  board's existing Last Name field are supported, with whitespace/case normalization.
- Re-reads the target immediately before writing. Duplicate/paged/incomplete
  lookups, changed identity, mismatched names, wrong boards and inactive candidates
  are held without a write.
- Updates only Reference automation update with counts, per-referee request state,
  confirmed send counts and administrative review notes. No email addresses or
  reference bodies are copied. Existing References Received, References Confirmed,
  Employment Status, DBS and hiring fields are not written.
- Received means received and unverified. Professional suitability and employment
  decisions remain with staff.
- Verifies the updated summary after mutation. An unchanged summary is not rewritten;
  an uncertain board result can safely retry the same summary after identity checks.
- Saves a separate source/item/result/reason audit in the reference ledger. The
  protected review queue exposes held mapping records; protected worker status shows
  board-sync counts/error. No public MCP method or generic board writer is added.
- Board errors do not stop reference receipt scanning or undo confirmed email sends.
  Read/check/write is not a conditional atomic Monday mutation; a staff edit between
  the final check and mutation may briefly race with one summary write. Read-back
  detects mismatched identities and holds the mapping for review.

## Configuration and rollout

All new flags default false:

- ESCS_REFERENCE_MONDAY_SYNC_ENABLED=false
- ESCS_REFERENCE_MONDAY_WRITE_ENABLED=false
- MONDAY_RECRUITMENT_API_TOKEN=<separately approved stored connection>

After owner approval, store the connection securely and deploy this draft. Start
with sync enabled and writes false. Check live API schema, verified source/item
mapping and a controlled test. Only then enable board writes. A token is sent in
the Authorization header to https://api.monday.com/v2, never in code, URLs or errors.
API-Version is pinned to 2026-10. The fixed source/summary column IDs are verified
through the live connector; schema changes block updates.

## Validation

75 tests passed across the board draft, reference engine and mailbox reader.
Tests cover exact matching, first/last name handling, preview mode, summary-only
mutation, idempotence, missing/duplicate links, incomplete paging, wrong board/name,
employed candidates, changed source links, schema drift, safe provider errors,
unverified receipt wording and isolation of board failures from reference processing.
No live board summaries were written and no new test email was sent for this draft.
Live token-based GraphQL and end-to-end board tests remain pending.

Official interfaces:

- https://developer.monday.com/api-reference/reference/items-page-by-column-values
- https://developer.monday.com/api-reference/reference/long-text
- https://developer.monday.com/api-reference/docs/api-versioning
