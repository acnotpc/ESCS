# ESCS Findmyshift Read-Only Connector

A small FastAPI service for the ESCS Primary Checks workflow.

## What it exposes

- `GET /teams`
- `GET /staff?teamId=...`
- `GET /facilities?teamId=...`
- `GET /shifts?teamId=...&from=YYYY-MM-DD&to=YYYY-MM-DD`
- `GET /time-off?teamId=...&from=YYYY-MM-DD&to=YYYY-MM-DD`
- `GET /primary-check-data?teamId=...&date=YYYY-MM-DD`
- `GET /health`

No Findmyshift write endpoint is implemented.

## OAuth

1. Deploy this app to an HTTPS host.
2. Set `FMS_REDIRECT_URI` to `https://YOUR-HOST/oauth/callback`.
3. Put that exact URI in the Findmyshift application's **Redirect URIs** field.
4. Set `FMS_CLIENT_ID` and `FMS_CLIENT_SECRET` as host environment variables/secrets.
5. Set a long random `SESSION_SECRET`.
6. Open `https://YOUR-HOST/oauth/start` and approve access.

**Never put the Client Secret in ChatGPT or source control.**

## Local run

```bash
python -m venv .venv
# activate the venv
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

For OAuth, localhost is only useful if Findmyshift accepts the exact callback URI and the browser can reach it. Production should use HTTPS.

## Production hardening before live use

The included token store is deliberately minimal and in-memory. Before production:
- persist OAuth tokens in an encrypted secret/database store;
- implement refresh-token handling using Findmyshift's token endpoint;
- protect connector endpoints with authentication so they are not public;
- restrict inbound access where possible;
- add audit logging without logging tokens or sensitive staff data;
- handle 429 responses with backoff;
- keep requests serial because Findmyshift documents one concurrent API request per application/API key.

## Primary Checks integration

The `/primary-check-data` endpoint bundles staff, facilities, shifts and time-off for one day. It is intended to be reconciled against the connected monday.com boards used by ESCS. The first production version should remain read-only and only surface discrepancies.
