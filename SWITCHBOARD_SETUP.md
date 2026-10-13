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

## Native call pop-ups and required outcomes

The dedicated OAuth application is disabled by default until installation and routing are verified.

- App name: ESCS Call Actions.
- Interface handler: `https://escs.onrender.com/switchboard/app/frame`.
- Installation handler: `/switchboard/app/install/<private installation capability>`. Never commit its concrete URL.
- Scopes: CRM, Telephony, widgets (`placement`) and brief read-only user directory (`user_brief`). Install as Andy Challoner, user 1.
- Store OAuth client secret in `SWITCHBOARD_APP_CLIENT_SECRET` on Render. Tokens are encrypted on the persistent disk using the separate `SWITCHBOARD_APP_SESSION_SECRET`.
- Installation registers a CALL_CARD tab. Caller name, enquiry summary and outcome are required. Card dismissal does not close the application worklist; callbacks and transfers stay outstanding until resolved.
- Exact active shared accounts route options 1/2/5 to Control Room Team, 3 to Accounts Team and 4 to Recruitment Administrator. Missing or ambiguous recipients block delivery. HR call actions are visible only to their assigned recipient and Andy through this application.
- Only the ESCS line is eligible. Other account lines do not receive ESCS pop-ups.
- Routing remains unconfigured. `SWITCHBOARD_POPUP_MENU_FIELD` must identify a verified field containing the option number, or `SWITCHBOARD_POPUP_ANSWERER_MAP` must contain verified unique answerer identifiers. Unknown routes are never guessed.
- Detailed setup status: `/switchboard/app/admin-status`, protected by the separate app secret in `x-sbf-app-key`. Public app health exposes booleans only.
- The original bridge records received field names in protected diagnostics to discover missing menu metadata; unknown field values are discarded.
- Existing call logging and contact creation continue independently of pop-up readiness.

Validation: `python -m unittest test_switchboard_bridge test_switchboard_popups -v` (21 passing tests before installation).

HR enquiry summaries stay in the restricted application worklist and are not copied into general CRM timelines or reminders until separate HR CRM access controls are verified.
