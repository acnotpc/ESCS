import os
import secrets
from urllib.parse import urlencode
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse, JSONResponse

app = FastAPI(
    title="ESCS Findmyshift Read-Only Connector",
    version="0.1.0",
    description="Read-only connector for ESCS Primary Checks."
)

FMS_BASE = "https://www.findmyshift.com"
API_BASE = f"{FMS_BASE}/api/1.4"
AUTH_URL = f"{FMS_BASE}/oauth2-login"
TOKEN_URL = f"{FMS_BASE}/oauth2-token"

CLIENT_ID = os.getenv("FMS_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("FMS_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("FMS_REDIRECT_URI", "")

# Demo token store. Replace with encrypted persistent storage before production use.
TOKENS = {}
OAUTH_STATES = set()

def require_config():
    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        raise HTTPException(500, "OAuth environment variables are not configured.")

def access_token():
    token = TOKENS.get("default", {}).get("access_token")
    if not token:
        raise HTTPException(401, "Findmyshift is not connected. Visit /oauth/start first.")
    return token

async def fms_get(path: str, params: dict | None = None):
    # Findmyshift accepts OAuth access tokens in place of API keys.
    # Keep authentication server-side; never return secrets to clients.
    p = dict(params or {})
    p["apiKey"] = access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{API_BASE}/{path}", params=p)
    if r.status_code == 429:
        raise HTTPException(429, "Findmyshift rate limit reached; retry later.")
    if not r.is_success:
        raise HTTPException(r.status_code, f"Findmyshift API error: {r.text[:500]}")
    try:
        return r.json()
    except Exception:
        return {"raw": r.text}

@app.get("/health")
async def health():
    return {"ok": True, "connected": bool(TOKENS.get("default", {}).get("access_token"))}

@app.get("/oauth/start")
async def oauth_start():
    require_config()
    state = secrets.token_urlsafe(32)
    OAUTH_STATES.add(state)
    # Findmyshift documentation requires client id + redirect URI for authorization.
    qs = urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "state": state,
    })
    return RedirectResponse(f"{AUTH_URL}?{qs}")

@app.get("/oauth/callback")
async def oauth_callback(code: str, state: str | None = None):
    require_config()
    if state and state not in OAUTH_STATES:
        raise HTTPException(400, "Invalid OAuth state.")
    if state:
        OAUTH_STATES.discard(state)

    # Findmyshift documents this exchange as a server-side POST.
    params = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(TOKEN_URL, params=params)
    if not r.is_success:
        raise HTTPException(r.status_code, f"OAuth token exchange failed: {r.text[:500]}")
    data = r.json()
    TOKENS["default"] = data
    return JSONResponse({"connected": True, "message": "Findmyshift connected. Secrets are stored server-side."})

@app.get("/teams")
async def teams():
    return await fms_get("teams/list")

@app.get("/staff")
async def staff(teamId: str | None = None):
    return await fms_get("staff/list", {"teamId": teamId} if teamId else {})

@app.get("/facilities")
async def facilities(teamId: str = Query(...)):
    return await fms_get("facilities/list", {"teamId": teamId})

@app.get("/shifts")
async def shifts(
    teamId: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    publishedShifts: str = "yes",
):
    return await fms_get("reports/shifts", {
        "teamId": teamId,
        "from": from_,
        "to": to,
        "publishedShifts": publishedShifts,
        "comments": "yes",
        "times": "yes",
        "facilities": "yes",
        "groupByStaff": "yes",
    })

@app.get("/time-off")
async def time_off(teamId: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    return await fms_get("time-off/list", {"teamId": teamId, "from": from_, "to": to})

@app.get("/primary-check-data")
async def primary_check_data(teamId: str, date: str):
    """Bundle the read-only FMS data needed for a daily Primary Check."""
    staff_data = await fms_get("staff/list", {"teamId": teamId})
    facilities_data = await fms_get("facilities/list", {"teamId": teamId})
    shifts_data = await fms_get("reports/shifts", {
        "teamId": teamId, "from": date, "to": date,
        "publishedShifts": "yes", "comments": "yes",
        "times": "yes", "facilities": "yes", "groupByStaff": "yes",
    })
    timeoff_data = await fms_get("time-off/list", {"teamId": teamId, "from": date, "to": date})
    return {
        "date": date,
        "teamId": teamId,
        "staff": staff_data,
        "facilities": facilities_data,
        "shifts": shifts_data,
        "timeOff": timeoff_data,
    }
