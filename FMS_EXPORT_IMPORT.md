# Findmyshift colour-preserving export import

## HTML exports retain individual overrides

The existing MCP connector also exposes `preview_fms_html_rota(html,
observed_at, service_date)`. This read-only action accepts the actual UTF-8
HTML export through the connector, fetches current staff for the fixed ESCS
team and returns the requested date plus its next date. It needs no exposed
connector key. It does not persist evidence, change plans or publish messages.
The original seven-date coverage and `next_day_covered` are explicit; Sundays
still require the following week's export. Upload limits and parser/freshness
checks remain enforced. No credentials or permissions are added. Refresh the
installed connector's tools after deployment to expose the new action.

Use the protected `POST /team-sync/daily/html-import` route for a static FMS
HTML weekly-view download. Send UTF-8 file bytes as the raw body, with the same
`observed_at` and optional `save_evidence=true` parameters as the Excel route.
The default remains preview only; it never changes plans, posts or allocations.

The 12–18 October HTML download preserves each person's explicit inline
background separately from its parent group cell. The importer reads these
individual styles, including three-digit RGB colours, rather than inheriting
the surrounding group colour. Missing or unknown styles remain unverified and
require review. It still matches names to unique active FMS staff IDs, checks
all four groups and continuous dates, and retains staff source locators,
commitment evidence and next-day markers. Synthetic cell references describe
the expanded extraction; `source_locator` identifies the original staff element
in the downloaded HTML table. Counts are marked `individual_colour_markers`.

`individual_colours_verified=true` means the extracted active staff markers
are explicit and recognised, with no unresolved identity matches. It is not a
confirmation of complete staffing capacity or dispatch readiness. Hours, rest,
release, map membership, vehicle suitability and other commitments still need
verification. `dispatch_ready` and `automatic_collection_configured` remain
false. The last service date still needs the following week for next-day checks.

HTML is parsed without execution or fetching resources. Upload size, nesting,
node count, table/date layout and unsupported active content are bounded or
rejected. Authentication, capture-time freshness, durable digest storage and
anti-recapture checks are shared with Excel. Existing credentials are unchanged.
Automatic fresh HTML collection is not configured.

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

Coloured staff cells in the four group sections establish export markers only.
The 9 October export retained Gagandeep's group blue and Colin's group pink,
while their individual live colours were holiday and blue respectively. Daniel
and Jacek had white export cells despite light-blue and blue live markers.
Individual colours take priority; this file format cannot establish them alone.

Every active record therefore has `individual_colour_verified=false` and an
`individual_colour_verification_required` review issue. The result also returns
`individual_colours_verified=false`, a colour warning, and
`counts_scope=export_cell_markers_only` for each day. Counts describe extracted
fills, not confirmed available staff. Next-day markers also require individual
verification before bold highlighting. A blue heading or training-room cell is
not staff availability.
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

Run `python -m unittest -q test_fms_xlsx test_fms_html test_daily_teams test_team_sync
test_timing_sync` in the existing service environment. Tests use synthetic
records and cover colour handling, active/ambiguous identities, next-day
markers, unmarked entries, stale timestamps, malformed archives, auth,
idempotent evidence storage, and preservation of plans and Feed posts.


