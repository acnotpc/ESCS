# SwitchboardFREE to ESCS Bitrix24

Adds call metadata to existing contact/company timelines and provisional contacts for unmatched telephone numbers. It does not create leads, deals, inferred companies, recordings, transcripts or send messages.

Uses the existing ESCS Render service and persistent disk. Route: `/switchboard/events`.

Environment:
- `SWITCHBOARD_ENABLED=true`
- `SWITCHBOARD_WRITE_ENABLED=false` for first demo; true only after verification
- `SWITCHBOARD_WEBHOOK_SECRET`: random secret, stored only in Render and SwitchboardFREE
- `SWITCHBOARD_STATE_DB=/var/data/switchboard.sqlite3`
- `SWITCHBOARD_BITRIX_REST_URL`: Andy Challoner user 1 webhook with CRM permissions. Falls back to existing `BITRIX_REST_WEBHOOK_URL`.

Enable inbound start/answered/complete and outbound start/answered/end in SwitchboardFREE. Outbound end lacks the number; start events are essential. The same call reference joins events. Use the shared secret field. Keep account, billing and keyword/transcript events disabled.

`/switchboard/status` is protected by the existing connector key. `/switchboard/health` reveals only configuration/readiness flags. Test via SwitchboardFREE's Send demo with writes disabled. Preview rows never become live writes automatically.

Numbers normalize to international format, with UK national numbers using +44. Exact number matches reuse CRM contacts and companies. Multiple contacts or companies and conflicting associations require review. Company-only matches receive company timeline entries without creating a fake person. Single verified existing contact/company links receive both timeline entries. Unknown numbers get a closed provisional contact assigned to user 1; staff must verify its identity and organisation.

Persistent call references prevent repeat processing. A durable write intent is saved before each CRM write. An uncertain external write is held for review rather than replayed, preventing duplicates after timeouts/restarts. Read failures remain pending for retry. Missing outbound start events wait ten minutes before review. Withheld/invalid numbers remain in review and never create contacts.

Operations: monitor protected status counts and last_error. Review rows require reconciliation with Bitrix timelines before any manual retry; no automatic merge or forced association is provided. Retention for the call ledger must be agreed separately; no automatic purge runs in this initial version. SQLite contains only allowlisted call metadata. Use a single service process/instance with the persistent disk.

Validation: `python -m unittest test_switchboard_bridge -v`.
