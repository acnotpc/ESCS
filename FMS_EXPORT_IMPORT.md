# Findmyshift colour-preserving export import

The private `POST /team-sync/daily/xlsx-import` endpoint reads a one-week
facility-view XLSX export using the existing connector authentication. Send the
file as the raw request body, with `observed_at` as a timezone-aware query
parameter recording when the export was actually obtained from FMS. The
endpoint fetches current staff identities from the existing Exclusive FMS team.
It adds no credentials, public MCP tools, or recipients.

Default behaviour is preview only. `save_evidence=true` persists the sanitised
extraction to the existing SQLite state database; it does not store the XLSX
file, change daily plans, edit Feed posts, or allocate jobs. Matching requires
an exact normalised name and a unique current staff record. Known role suffixes
and typographic apostrophes are normalised. Inactive historical duplicates
cannot hide a unique active employee. Multiple active matches require review.

## What is retained

- Seven service dates and all four dated regular staff groups.
- Exact RGB fills, source cell references, staff IDs and active status.
- Next-calendar-day markers for the same staff ID, where the export covers it.
- Candidate commitment text and time evidence from non-group sections.
- Separate bank/overtime evidence, without treating a day-off marker or an
  appearance in an overtime section as confirmed working hours.
- Unmarked/unknown colours, overlapping sickness/training/other allocations,
  and active conveyance records outside the groups as review items.

Only coloured staff cells in the four group sections establish regular rota
markers. A blue heading or a training-room cell is not staff availability.
White cells are unknown. Absence of a known commitment does not prove a free
window. Other allocations require reconciliation even when a group cell is blue.
Map membership and C1 labels remain separate verified sources.

## Freshness and publication

Capture times must be timezone-aware and within the last 24 hours. An existing
saved digest cannot be stamped with a different capture time. Reusing an old
file cannot renew its evidence. A verified repeat capture with identical bytes
currently needs separate reconciliation; automatic renewal is not implemented.

There is deliberately no automatic conversion to `Plan`. Review conflicts,
confirm hours, duty/rest/release checks and the map, then supply a verified plan
through the existing protected `/team-sync/daily/plans` route. The existing
writer continues to protect operator-managed teams. The final date of a weekly
export needs the following week's export for next-day highlighting.

## Automatic collection remains outstanding

This endpoint accepts fresh files; it does not download them. FMS documents
manual schedule downloads, but a supported unattended colour-preserving export
route has not been confirmed. The browser session used for interactive checks
is not a configured server-side scheduled downloader.

Before an unattended collector is enabled, verify its authorised FMS access,
the actual facility-view download, date coverage, colour retention, genuine
capture time and a failed-login/stale-file alert. Do not substitute CSV, which
cannot preserve cell fills, or broaden access to a publicly shared rota.

## Validation

Run `python -m unittest -q test_fms_xlsx test_daily_teams test_team_sync
test_timing_sync` in the existing service environment. Tests use synthetic
records and cover colour handling, active/ambiguous identities, next-day
markers, unmarked entries, stale timestamps, malformed archives, auth,
idempotent evidence storage, and preservation of plans and Feed posts.
