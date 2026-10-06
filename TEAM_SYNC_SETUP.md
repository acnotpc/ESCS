# ESCS live teams-list integration

## Dated daily lists and advance preparation (6 October)

`daily_teams.py` discovers the rolling today/tomorrow/day-after-tomorrow lists
from the existing Hub Provision audience (`SG127`) with complete Feed pagination.
Titles must be `YYYY-MM-DD - Teams List`, optionally followed by the existing
uppercase author initials, for example ` - MB`. Control Room Team (`1037`)
remains the author; new posts explicitly target the existing audience, never
Bitrix's all-users default. Duplicate matches block that date. Existing titles,
manual content and live-status blocks are preserved. Date-matched bindings can
be retargeted to the discovered post; crew are never copied from yesterday.
Only explicitly verified bindings with `daily_route=true` participate; private
test and older bindings default to false and are never moved into a live list.

New configuration:

* `BITRIX_DAILY_TEAMS_ENABLED=true` enables discovery and an hourly refresh.
* `BITRIX_DAILY_TEAMS_WRITE_ENABLED=false` retains preview mode; `true` permits
  verified provisional-pool creation/update, independently of live chat writes.
* `BITRIX_DAILY_TEAMS_AUTHOR_ID=1037` and `BITRIX_DAILY_TEAMS_DEST=["SG127"]`
  pin the verified existing author and audience. Changing audience is a separate
  access decision; do not broaden it during routine rollover.

Protected routes (same `X-Connector-Key`): `PUT /team-sync/daily/plans` accepts
a complete dated roster snapshot; `POST /team-sync/daily/refresh` discovers and
prepares lists; `GET /team-sync/daily/status` reports refresh health. Snapshots
must be at most 24 hours old, date-matched, complete and free of duplicate staff
IDs. Missing snapshots stop creation. A roster row must explicitly confirm blue
availability, active status, availability interval, commitments and duty-history
checks before it appears in a provisional pool. Sick/holiday/training/pink or
inactive rows are excluded; uncertain eligible rows are marked for verification.
Confirmed commitments trim the free window; confirmed >12-hour duties trim it
by the 11-hour rest period. An unconfirmed final meeting return blocks eligibility.
Regular staff remain grouped using FMS Staff Groups 1–4; verified spare staff
remain separate. These are candidate pools, not selected job crews.

The service has no supported unattended source of per-cell FMS colours. The
hourly worker can route lists and publish verified supplied snapshots; it cannot
refresh colour evidence by itself. The required daily roster ingestion must be
completed before describing automatic team generation as active. It also cannot
choose a meeting point, check RAC or enforce future fatigue across a proposed
job's forecast without those inputs. Final dispatch checks remain required.

Creation intent is persisted before calling Bitrix. After a lost response,
rediscover a matching post; if no post is visible, block automatic re-creation
and reconcile manually. This prevents duplicate adds after uncertain timeouts.
The single-writer disk requirement and Bitrix's manual-edit race still apply.

Tests: `python -m unittest -v test_team_sync test_daily_teams`.

## Authenticated event queue (6 October activation)

The registered bot token authenticates REST calls but did not match Bitrix's
top-level webhook event token. Production can instead use the supported fetch
queue: set the existing bot's `eventMode` to `fetch`, and set
`BITRIX_TEAM_EVENT_MODE=fetch`. `BITRIX_TEAM_BOT_TOKEN` holds the registered bot
secret. For migration it falls back to the previously configured
`BITRIX_EVENT_APPLICATION_TOKEN`, which currently contains that bot secret.
The HTTP event receiver is disabled in fetch mode. No first-event trust or
relaxation of authentication is used.

The service polls `imbot.v2.Event.get` every 15 seconds with `withUserEvents=false`.
It persists sanitized milestones and pending writes before committing the queue
cursor to the mounted SQLite database. The next request acknowledges that cursor;
a restart/replayed batch cannot lose a pending write. Queue IDs order edits and
deletions even within one second. Malformed events stop the cursor and appear as
a generic error on the protected status endpoint. Unapproved message authors
remain excluded. Chat membership and daily bindings still require the procedure
below. Writes remain disabled until a real private-chat-to-Feed test succeeds.

## What this version does

Receives Bitrix supervisor-bot message events and edits the existing daily
teams-list feed post. Its live status section identifies the team, job, vehicle,
journey stage, and update time. Original team names, spare-staff entries, title,
recipients and attachments are preserved. It does not send chat replies, allocate
jobs, move Monday jobs, or declare a vehicle roadworthy.

This is a tested first implementation, not an activated production connection.
The existing Findmyshift integration and `/mcp` endpoint are retained. New
management endpoints require the existing `X-Connector-Key`; no team or chat
information is added to the public MCP tools.

## Required setup before activation

1. Attach persistent storage to the Render service. This implementation requires
   **one service instance and one writer**, using SQLite on the mounted disk.
   Do not put state on Render's ephemeral filesystem. Adding a disk may change
   billing; obtain the owner's approval before provisioning it. A multi-instance
   deployment requires a database and distributed locking instead.
2. Create a dedicated Bitrix API connection under an authorised administrator or
   the author of the teams-list posts, with only `imbot` and `log` permissions.
   Confirm that this account can read and edit the target posts. Do not use a
   general CRM or HR connection. Keep the inbound webhook URL in Render's secret
   environment settings; never paste credentials into chat or source control.
3. Register a visible `supervisor` bot named **ESCS Teams Status**, using
   `imbot.v2.Bot.register`, `eventMode: webhook`, and the event handler
   `https://escs.onrender.com/team-sync/bitrix-events`. Add it only to the dedicated
   operational job chats that are approved for monitoring. This grants the bot
   access to those chats, so the owner must explicitly approve the access.
   Register with a unique bot token of at most 40 characters if using inbound
   webhook authentication. Do not log registration request/response tokens.
4. Configure `BITRIX_EVENT_APPLICATION_TOKEN` with the **top-level event**
   application token provided by Bitrix, not the token nested in `data.bot.auth`.
   Never derive trust from the first event or use the callback's REST endpoint.
5. Set the other variables below, initially keeping write mode off.
6. Register verified bindings for the day's jobs. Read the Monday `Chat Link`,
   current crew and vehicle, and the day's actual teams list before doing this.
   Use the current feed POST_ID returned by `log.blogpost.get`; do not assume a
   previous day's post or a feed activity ID is the post ID. Confirm the mapping
   between canonical staff IDs and Bitrix users. A binding includes the first
   meeting time for **each** crew member across their entire duty period, and
   the IDs of permitted crew/control-room message authors. Do not reset first
   meeting times when jobs are chained. Daily binding discovery is not automated
   by this version; do not activate without a daily binding-management procedure.
7. Send a synthetic operational update in a separate test job chat and inspect
   the authenticated preview. Test add/edit/delete and a failed update/retry.
8. Verify a write to an approved test feed post. Only then bind live posts and
   enable writing. Monitor the protected status endpoint for pending failures.

## Environment variables

| Variable | Value |
| --- | --- |
| `BITRIX_TEAM_SYNC_ENABLED` | `true` after setup; absent means disabled |
| `BITRIX_TEAM_SYNC_WRITE_ENABLED` | `false` for preview; `true` after test write |
| `BITRIX_TEAM_STATE_DB` | Absolute path on the persistent disk, such as `/var/data/teams.sqlite` |
| `BITRIX_REST_WEBHOOK_URL` | Secret inbound REST webhook URL for **escs.bitrix24.com** |
| `BITRIX_EVENT_APPLICATION_TOKEN` | Secret top-level event application token |
| `BITRIX_TEAM_BOT_ID` | ID returned by the bot registration |
| `CONNECTOR_API_KEY` | Existing protected management API key |

## Endpoints

* `POST /team-sync/bitrix-events` accepts authenticated Bitrix v2 form or JSON
  events. Body size is bounded; domain, application token, bot, chat and author
  are checked. Credentials and patient text are never persisted by this module.
* `PUT /team-sync/bindings` saves a verified `Binding`:
  `chat_id`, `job_number` (`JOB` + five digits), `monday_item_id`, `post_id`,
  `service_date`, `team_label`, `vehicle`, `first_meetings` (staff ID to timestamp),
  `allowed_author_ids` (Bitrix IDs).
* `GET /team-sync/preview/{post_id}` previews sanitized status lines.
* `GET /team-sync/status` shows enabled/write state and pending updates.
* `POST /team-sync/retry` retries durable pending updates; a background loop also
  retries every 30 seconds while the service runs.
* `POST /team-sync/reconcile` accepts `chat_id`, `message_id`, verified `status`
  and timezone-aware `occurred` as query parameters to resolve a review after
  checking source chat/RAC. This does not replace a missing source check.

All endpoints except the event receiver require `X-Connector-Key`.

## Recognised messages

The initial parser recognises one operational milestone per message, with an
optional `HH:MM` or `HHMM` time before or after the milestone. Examples:

* `10:55 #ARRIVED - D1`
* `Departed D1 11:15`
* `Arrived at D2`
* `Departed D2`
* `Returned to D1`
* `Departed D1 RTN`
* `Returned to meeting point`
* `Crew released`
* `Job completed`

Times without a date use the London date of the original chat message. A time
later than the message, future claims, multiline reports, negation, predictions
and unfamiliar operational wording are marked for review. Non-operational
messages are ignored. Message edits and deletions mark the affected job for
review until reconciled; this prevents a retracted release remaining trusted.
Dates should be resolved manually around midnight when the message is backfilled.

## Availability and fatigue limits

* **Blue on FMS is available; pink is day off.** Map blue/pink markers instead
  describe male/female staff. FMS API colour access is still outstanding.
* Full-time on-call staff start at home from the conveyance map, with a preferred
  road journey of at most 30 minutes to their team's active meeting point.
* Actual operational updates override estimates. A stationary vehicle, a D2
  departure or `Job completed` never automatically releases crew.
* For a whole duty period exceeding 12 elapsed hours, allow 11 elapsed hours
  after confirmed final return to the meeting point before the next meeting.
  UTC arithmetic preserves elapsed rest across UK daylight-saving changes.
* Over 16 hours prompts an accommodation-offer/fatigue warning. It does not book
  accommodation or authorise further driving.
* Check **every member's** previous duties and upcoming commitments before a new
  allocation. This module displays fatigue flags; it does **not yet retrieve or
  enforce future Monday/FMS commitments**, automatically update home travel ETAs,
  or ingest RAC. Those remain explicit checks in the allocation workflow.
* Meetings/return/rest times must be verified. A crew release at D2 is not a
  confirmed final meeting return. Chained jobs require the same duty origin.

## Failure handling and validation

The durable store contains identifiers and sanitized milestone codes/timestamps,
not chat text. Repeated events are idempotent; older revisions cannot overwrite
newer ones. Publishing failures remain queued and retry after a restart. Ambiguous
status blocks, historical lists and uncertain edits need review. Only the managed
status block is replaced. Bitrix lacks an atomic compare-and-swap post edit:
re-read before update and verify after it, and avoid concurrent manual editing
during publication. This remaining race must be checked during the live trial.

Run `python -m unittest -v test_team_sync` for parser/authentication/replay,
sanitisation, concurrency, persistence, failure retry, and rest-boundary tests.

Official API references:

* https://apidocs.bitrix24.com/api-reference/chat-bots/chat-bots-v2/index.html
* https://apidocs.bitrix24.com/api-reference/chat-bots/chat-bots-v2/imbot.v2/bots/bot-register.html
* https://apidocs.bitrix24.com/api-reference/chat-bots/chat-bots-v2/imbot.v2/events/events.html
* https://apidocs.bitrix24.com/api-reference/log/log-blogpost-get.html
* https://apidocs.bitrix24.com/api-reference/log/log-blogpost-update.html
# Light-blue availability

Light-blue staff may appear in the provisional pool with a visible `Required for` caveat. Record each known meeting/training/other commitment as a dated interval with its purpose. The planner removes those intervals before presenting a free window. Missing commitment details or unverified duty history keep the person visible under verification, without treating them as cleared. Pink, sickness, holiday and training markers remain excluded from availability.

An explicit blue overtime entry can be recorded separately from a person's regular day-off marker, with `regular_group=null` and an `availability_note` describing the overtime limits. Do not treat a pink regular-group cell alone as available.

# Verified snapshot import

`BITRIX_DAILY_TEAMS_ROSTER_SNAPSHOT` accepts a JSON array of one to three existing Plan objects through Render's existing service administration. No new public MCP write tool or credential is added. Keep the actual timezone-aware observation time; do not refresh it on restart. The importer validates the entire batch, preserves newer protected imports, rejects conflicting snapshots and ignores expired/historical snapshots. A snapshot is valid for at most 24 hours, so this route supplies verified observations rather than unattended colour capture. Continue to refresh colours from FMS; the reports API does not expose them.

Pending availability/rest/release checks retain names under their regular staff group, with spares separate. Such entries are provisional and must not be treated as dispatch clearance. Optional plain `availability_note` text may describe known overtime limits or another non-patient operational commitment.

# Full-time map team-building process

Use the existing Full Time Conveyance Staff Google My Map together with the dated FMS roster. The map is the source for full-time membership, longest-serving-first order, broad locality and C1 labels; its blue/pink pins describe sex, not availability. FMS remains the source for availability, staff groups, overtime and all dated commitments. Exclude deleted/inactive staff and rota absences/day-off entries unless a separate explicit available overtime entry exists.

Verified plans can include `map_reference`, timezone-aware `map_checked_at` and each matched candidate's `map_profile` (`order`, broad `area`, and `c1`). Match by verified FMS identity, including known display-name variants. Do not store or publish home addresses or coordinates in this plan. Map evidence expires after 30 days; roster evidence still expires after 24 hours. Refresh the map whenever membership, order, location or licence labels change. Incomplete regular-staff matching produces a verification notice instead of invented teams.

The managed daily block now generates proposed Teams A–D from the available regular groups with at least two members, preserving each FMS group and listing members by map seniority. A single member appears as a full-time spare. Team lines use `Team A - Name (S), Name (P), Name (P) (C1)` with verified markers only. Locations and staff-group diagnostics are omitted from the displayed rows. Known commitments and overtime hours remain in separate notes and in the allocation data. Single members remain separate and may only be joined with suitable available staff; Michel Nindorera is separate from Team A and may work alone or with verified Manchester-area staff. Explicit overtime candidates use `overtime=true` with `regular_group=null` and remain separate; bank staff remain in the spare pool. No overtime is inferred from the map or a day-off marker.

These are provisional groupings, not job-specific crews or dispatch clearance. Confirm booking headcount, driver/vehicle approval, every member's availability and rest, actual crew release, active meeting points and home-to-meeting road travel before allocation. Do not infer travel duration from locality or automatically assign leadership from map order. The existing daily writer reuses the exact dated Feed post and replaces only its managed provisional block. Automatic fresh FMS colour capture is still outstanding; this process runs when verified roster/map evidence is supplied.
