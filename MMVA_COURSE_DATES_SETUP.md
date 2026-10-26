# MMVA course-date maintenance

Isolated within the existing ESCS Render process; no new Render service is required.

## Configuration

Set these keys on the existing Render ESCS service. Never commit or send the token in chat.

| Key | Value |
| --- | --- |
| `MMVA_ECWID_API_TOKEN` | Ecwid secret token for MMVA store 76123907, with catalogue read/update access |
| `MMVA_COURSE_DATES_ENABLED` | `true` after a successful preview |
| `MMVA_COURSE_DATES_WRITE_ENABLED` | `true` after preview and first write verification |
| `MMVA_COURSE_DATES_DB` | Optional; defaults to `/var/data/mmva_course_dates.sqlite3` on the existing disk |

Both enable flags default to false. No token means no Ecwid requests or changes.
Only product catalogue endpoints for MMVA store 76123907 are implemented. Orders,
customers, payments, pricing, product availability and stock are not written.

## Daily rule

After 06:00 Europe/London, check all products once per local day. A date choice
expires when its start day is before today. Preserve today's and future choices.
Recognise Date, Dates, Course date(s), and Training date(s) dropdown/radio/checkbox
options. Unrecognised date text, products with variations, and options with no
remaining future dates need review. Do not invent replacement dates or silently
remove the required date option. The Champions prerequisite question is not a date.

A restart does not repeat a completed daily run. Failed source reads retry after
five minutes. Durable before/after options are saved before catalogue writes.
Readback mismatches or uncertain writes pause subsequent changes. Product prices
and availability are checked after writes. No external messages are sent.

## Protected operator endpoints

All require the existing `X-Connector-Key` header; never put it in a URL.

- GET `/mmva/course-dates/status`
- POST `/mmva/course-dates/preview`: catalogue reads, no Ecwid writes
- POST `/mmva/course-dates/run`: requires both enable flags
- POST `/mmva/course-dates/reconcile`: reads Ecwid and classifies uncertain prior
  updates; clears the persistent pause only when all outcomes match known before
  or after options. Does not write to Ecwid.

MCP exposes read-only status and preview tools through the existing ESCS
connection. Catalogue writes run only in the background worker or through the
protected operator endpoint; no unauthenticated MCP write tool is added. Source and audit history are kept in the private persistent database.
A pending write blocks further runs until reconciliation. The SQLite lease also
prevents overlapping processes. Do not delete the audit database to clear a pause.

## Activation

1. Add the token securely in Render; keep both flags false initially.
2. Run the protected preview and review exact candidate removals.
3. Enable both flags and deploy the updated configuration.
4. Run one explicit check and read back results, prices and date options.
5. Confirm the daily worker's status and audit. No ChatGPT Site sign-in is needed.

## Tests

`python -m unittest test_mmva_course_dates`
