# ESCS live teams-list integration

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
