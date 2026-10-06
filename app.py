import os
import secrets
from urllib.parse import urlencode
import httpx
from fastapi import FastAPI, HTTPException, Query, Header, Depends
from fastapi.responses import RedirectResponse, JSONResponse
from contextlib import asynccontextmanager

from mcp_server import mcp
from team_sync import Settings, Synchronizer, build_router

FMS_BASE="https://www.findmyshift.com"
API_BASE=f"{FMS_BASE}/api/1.4"
AUTH_URL=f"{FMS_BASE}/oauth2-login"
TOKEN_URL=f"{FMS_BASE}/oauth2-token"
CLIENT_ID=os.getenv("FMS_CLIENT_ID","")
CLIENT_SECRET=os.getenv("FMS_CLIENT_SECRET","")
REDIRECT_URI=os.getenv("FMS_REDIRECT_URI","")
CONNECTOR_API_KEY=os.getenv("CONNECTOR_API_KEY","")
FMS_API_KEY=os.getenv("FMS_API_KEY","")
TOKENS={}
OAUTH_STATES=set()

def require_connector_key(x_connector_key: str|None=Header(default=None)):
    if not CONNECTOR_API_KEY:
        raise HTTPException(503,"Connector API protection is not configured.")
    if not x_connector_key or not secrets.compare_digest(x_connector_key,CONNECTOR_API_KEY):
        raise HTTPException(401,"Unauthorized.")

def require_config():
    if not CLIENT_ID or not CLIENT_SECRET or not REDIRECT_URI:
        raise HTTPException(500,"OAuth environment variables are not configured.")

def access_token():
    token=TOKENS.get("default",{}).get("access_token")
    if not token:
        raise HTTPException(401,"Findmyshift is not connected. Visit /oauth/start first.")
    return token

async def fms_get(path,params=None):
    p=dict(params or {})
    # For this first-party ESCS integration, prefer the dedicated Findmyshift
    # API key stored only in Render. OAuth remains available as a fallback.
    credential = FMS_API_KEY or access_token()
    p["apiKey"] = credential
    async with httpx.AsyncClient(timeout=30) as client:
        r=await client.get(f"{API_BASE}/{path}", params=p)
    if r.status_code==429:
        raise HTTPException(429,"Findmyshift rate limit reached; retry later.")
    if not r.is_success:
        raise HTTPException(r.status_code,f"Findmyshift API error: {r.text[:500]}")
    try:
        return r.json()
    except Exception:
        return {"raw":r.text}

# MCP tools call the same in-process Findmyshift client. No internal HTTP hop,
# and no connector key is exposed to ChatGPT.
@mcp.tool()
async def list_fms_teams():
    """List Findmyshift teams available to the ESCS account."""
    return await fms_get("teams/list")

@mcp.tool()
async def list_fms_staff(team_id: str):
    """List staff for a Findmyshift team."""
    return await fms_get("staff/list", {"teamId": team_id})

@mcp.tool()
async def list_fms_facilities(team_id: str):
    """List facilities/placements for a Findmyshift team."""
    return await fms_get("facilities/list", {"teamId": team_id})

@mcp.tool()
async def get_fms_shifts(team_id: str, date_from: str, date_to: str):
    """Get published shifts, times, facilities and comments for a date range (YYYY-MM-DD)."""
    return await fms_get("reports/shifts", {
        "teamId": team_id, "from": date_from, "to": date_to,
        "publishedShifts": "yes", "comments": "yes", "times": "yes",
        "facilities": "yes", "groupByStaff": "yes"
    })

@mcp.tool()
async def get_fms_time_off(team_id: str, date_from: str, date_to: str):
    """Get Findmyshift time-off records for a date range (YYYY-MM-DD)."""
    return await fms_get("time-off/list", {
        "teamId": team_id, "from": date_from, "to": date_to
    })

@mcp.tool()
async def get_conveyance_capacity(team_id: str, date: str):
    """Return FMS conveyance/bank candidates and their same-day commitments for Primary Checks.

    Important: Findmyshift's documented reports/shifts API does not expose the
    visual per-cell rota colour. This tool therefore does not infer blue/pink.
    It returns evidence for each candidate so the caller can cross-reference
    supported availability markers separately.
    """
    shifts = await fms_get("reports/shifts", {
        "teamId": team_id, "from": date, "to": date,
        "publishedShifts": "yes", "comments": "yes", "times": "yes",
        "facilities": "yes", "groupByStaff": "yes"
    })
    time_off = await fms_get("time-off/list", {
        "teamId": team_id, "from": date, "to": date
    })
    facilities = await fms_get("facilities/list", {"teamId": team_id})

    facility_names = {}
    facility_rows = facilities if isinstance(facilities, list) else [facilities]
    for f in facility_rows:
        if isinstance(f, dict) and f.get("facilityId"):
            facility_names[f["facilityId"]] = f.get("name")

    shift_rows = shifts if isinstance(shifts, list) else [shifts]
    by_staff = {}
    candidate_terms = (
        "conveyance", "bank staff", "available full time",
        "emergency bedwatch", "any -"
    )
    for row in shift_rows:
        if not isinstance(row, dict) or not row.get("staffId"):
            continue
        sid = row["staffId"]
        facility_name = facility_names.get(row.get("facilityId"))
        evidence = " ".join(filter(None, [row.get("shift"), facility_name])).lower()
        item = by_staff.setdefault(sid, {
            "staffId": sid,
            "name": " ".join(filter(None, [row.get("firstName"), row.get("lastName")])).strip(),
            "candidate": False,
            "entries": []
        })
        item["entries"].append({
            "shift": row.get("shift"),
            "facilityId": row.get("facilityId"),
            "facility": facility_name
        })
        if any(term in evidence for term in candidate_terms):
            item["candidate"] = True

    active_time_off = {}
    time_rows = time_off if isinstance(time_off, list) else [time_off]
    for row in time_rows:
        if not isinstance(row, dict) or row.get("dateDeleted"):
            continue
        sid = row.get("staffId")
        if sid:
            active_time_off.setdefault(sid, []).append({
                "type": row.get("type"),
                "description": row.get("description"),
                "firstDayOff": row.get("firstDayOff"),
                "lastDayOff": row.get("lastDayOff"),
                "style": row.get("style")
            })

    candidates = []
    for sid, item in by_staff.items():
        if item["candidate"]:
            item["activeTimeOff"] = active_time_off.get(sid, [])
            item["cellColourAvailable"] = False
            candidates.append(item)

    return {
        "date": date,
        "teamId": team_id,
        "candidateCountFromTextAndFacilities": len(candidates),
        "candidates": candidates,
        "colourLimitation": (
            "Findmyshift reports/shifts does not document per-cell rota colour/style; "
            "do not infer blue Available or pink Day Off from this result."
        )
    }

@mcp.tool()
async def get_primary_check_data(team_id: str, date: str):
    """Get the combined read-only FMS dataset for an ESCS Primary Check on YYYY-MM-DD."""
    return {
        "date": date,
        "teamId": team_id,
        "staff": await fms_get("staff/list", {"teamId": team_id}),
        "facilities": await fms_get("facilities/list", {"teamId": team_id}),
        "shifts": await fms_get("reports/shifts", {
            "teamId": team_id, "from": date, "to": date,
            "publishedShifts": "yes", "comments": "yes",
            "times": "yes", "facilities": "yes", "groupByStaff": "yes"
        }),
        "timeOff": await fms_get("time-off/list", {
            "teamId": team_id, "from": date, "to": date
        })
    }

mcp_app = mcp.streamable_http_app()

@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        async def retry_team_updates():
            import asyncio
            while True:
                await asyncio.sleep(30)
                if team_sync.settings.enabled:
                    try:
                        await team_sync.flush()
                    except Exception:
                        # Status endpoint reports setup/pending state. Never log credentials.
                        pass
        import asyncio
        retry_task = asyncio.create_task(retry_team_updates())
        try:
            yield
        finally:
            retry_task.cancel()
            try:
                await retry_task
            except asyncio.CancelledError:
                pass

app = FastAPI(title="ESCS Findmyshift Read-Only Connector", version="0.6.0", lifespan=lifespan)
team_sync = Synchronizer(Settings())
app.include_router(build_router(team_sync, require_connector_key))

@app.get("/health")
async def health():
    return {"ok":True,"connected":bool(TOKENS.get("default",{}).get("access_token"))}

@app.get("/oauth/start")
async def oauth_start():
    require_config()
    state=secrets.token_urlsafe(32)
    OAUTH_STATES.add(state)
    qs=urlencode({"client_id":CLIENT_ID,"redirect_uri":REDIRECT_URI,"response_type":"code","state":state})
    return RedirectResponse(f"{AUTH_URL}?{qs}")

@app.get("/oauth/callback")
async def oauth_callback(code:str,state:str|None=None):
    require_config()
    if state and state not in OAUTH_STATES:
        raise HTTPException(400,"Invalid OAuth state.")
    if state:
        OAUTH_STATES.discard(state)
    params={"client_id":CLIENT_ID,"client_secret":CLIENT_SECRET,"code":code,
            "grant_type":"authorization_code","redirect_uri":REDIRECT_URI}
    async with httpx.AsyncClient(timeout=30) as client:
        r=await client.post(TOKEN_URL,params=params)
    if not r.is_success:
        raise HTTPException(r.status_code,f"OAuth token exchange failed: {r.text[:500]}")
    TOKENS["default"]=r.json()
    return JSONResponse({"connected":True,"message":"Findmyshift connected."})

@app.get("/teams",dependencies=[Depends(require_connector_key)])
async def teams():
    return await fms_get("teams/list")

@app.get("/staff",dependencies=[Depends(require_connector_key)])
async def staff(teamId:str|None=None):
    return await fms_get("staff/list",{"teamId":teamId} if teamId else {})

@app.get("/facilities",dependencies=[Depends(require_connector_key)])
async def facilities(teamId:str=Query(...)):
    return await fms_get("facilities/list",{"teamId":teamId})

@app.get("/shifts",dependencies=[Depends(require_connector_key)])
async def shifts(teamId:str,from_:str=Query(...,alias="from"),to:str=Query(...),publishedShifts:str="yes"):
    return await fms_get("reports/shifts",{
        "teamId":teamId,"from":from_,"to":to,"publishedShifts":publishedShifts,
        "comments":"yes","times":"yes","facilities":"yes","groupByStaff":"yes"
    })

@app.get("/time-off",dependencies=[Depends(require_connector_key)])
async def time_off(teamId:str,from_:str=Query(...,alias="from"),to:str=Query(...)):
    return await fms_get("time-off/list",{"teamId":teamId,"from":from_,"to":to})

@app.get("/primary-check-data",dependencies=[Depends(require_connector_key)])
async def primary_check_data(teamId:str,date:str):
    return await get_primary_check_data(teamId,date)

# Preserve the external MCP endpoint as https://escs.onrender.com/mcp
app.mount("/", mcp_app)
