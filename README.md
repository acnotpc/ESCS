# ESCS Findmyshift Read-Only Connector

Secure read-only Findmyshift connector for the ESCS Primary Checks workflow.

## Data endpoints
- GET /teams
- GET /staff?teamId=...
- GET /facilities?teamId=...
- GET /shifts?teamId=...&from=YYYY-MM-DD&to=YYYY-MM-DD
- GET /time-off?teamId=...&from=YYYY-MM-DD&to=YYYY-MM-DD
- GET /primary-check-data?teamId=...&date=YYYY-MM-DD
- GET /health

No Findmyshift write endpoint is implemented.

## Security
Set CONNECTOR_API_KEY in Render to a long random value. All staff/rota data endpoints require the HTTP header X-Connector-Key. Do not place this key in source control, URLs, screenshots, or chat messages.

/health, /oauth/start and /oauth/callback remain public for health checking and OAuth.

## Render environment variables
FMS_CLIENT_ID
FMS_CLIENT_SECRET
FMS_REDIRECT_URI=https://escs.onrender.com/oauth/callback
CONNECTOR_API_KEY
SESSION_SECRET
PORT=8000

## Important production note
OAuth tokens are currently held in memory. A Render restart clears them and requires /oauth/start authorization again. Persistent encrypted token storage and refresh-token handling should be added before relying on unattended operation.
