import os
import re
import logging
import secrets
from urllib.parse import urlencode
import httpx
from fastapi import FastAPI, HTTPException, Query, Header, Depends
from fastapi.responses import RedirectResponse, JSONResponse
from contextlib import asynccontextmanager

from mcp_server import mcp
from team_sync import Settings, Synchronizer, build_router, api_call
from daily_teams import DailyTeams, build_daily_router

# REST credentials can be embedded in Bitrix URLs and Findmyshift query strings.
# Keep request URLs out of application logs; operational errors remain recorded.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

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

def _rows(payload):
    """Normalise Findmyshift list/report responses into a list of dictionaries."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []

@mcp.tool()
async def get_primary_check_summary(team_id: str, date: str):
    """Return a compact FMS-only Primary Check summary for YYYY-MM-DD.

    Processes the large Findmyshift shift report server-side and returns only
    actionable evidence: placement allocations, conveyance/bank candidates,
    agency/DRI allocations, Hub cover, absence/training/AWOL markers, and
    active time off. Day-off status is not inferred because the documented
    reports/shifts API does not expose the visual per-cell pink/blue colour.
    """
    shifts = _rows(await fms_get("reports/shifts", {
        "teamId": team_id, "from": date, "to": date,
        "publishedShifts": "yes", "comments": "yes", "times": "yes",
        "facilities": "yes", "groupByStaff": "yes"
    }))
    time_off = _rows(await fms_get("time-off/list", {
        "teamId": team_id, "from": date, "to": date
    }))

    by_staff = {}
    for row in shifts:
        sid = row.get("staffId")
        if not sid:
            continue
        item = by_staff.setdefault(sid, {
            "staffId": sid,
            "name": " ".join(filter(None, [row.get("firstName"), row.get("lastName")])).strip(),
            "entries": []
        })
        value = (row.get("shift") or "").strip()
        if value and value not in item["entries"]:
            item["entries"].append(value)

    active_off = {}
    for row in time_off:
        if row.get("dateDeleted"):
            continue
        sid = row.get("staffId")
        if sid:
            active_off.setdefault(sid, []).append({
                "type": row.get("type"),
                "description": row.get("description"),
                "firstDayOff": row.get("firstDayOff"),
                "lastDayOff": row.get("lastDayOff")
            })

    placement, conveyance, agency, hub, exceptions = [], [], [], [], []
    for sid, item in by_staff.items():
        entries = item["entries"]
        low = " | ".join(entries).lower()
        record = {"staffId": sid, "name": item["name"], "entries": entries}
        if sid in active_off:
            record["activeTimeOff"] = active_off[sid]

        placement_entries = [e for e in entries if e.lower().startswith("placement days ") or e.lower().startswith("placement nights ")]
        if placement_entries:
            # A placement pool entry is not itself proof of a final allocation.
            other = [e for e in entries if e not in placement_entries]
            placement.append({
                "staffId": sid, "name": item["name"],
                "placementPools": placement_entries,
                "otherEntries": other,
                "hasOtherAllocationEvidence": bool(other)
            })

        if any(t in low for t in ("conveyance", "bank staff", "available full time", "any -", "any-")):
            # Avoid false positives where 'conveyance' occurs only in an instruction.
            explicit = [e for e in entries if any(t in e.lower() for t in (
                "conveyance", "bank staff", "available full time", "any -", "any-"
            )) and "use conveyance staff" not in e.lower()]
            if explicit:
                conveyance.append({
                    "staffId": sid, "name": item["name"],
                    "evidence": explicit,
                    "allEntries": entries,
                    "activeTimeOff": active_off.get(sid, [])
                })

        if any(t in low for t in ("saba -", "dri -", "ward ")):
            if "dri" in low or "saba -" in low:
                agency.append(record)

        if "hub week" in low or "on call" in low:
            hub.append(record)

        markers = []
        for label in ("sickness", "holiday", "training", "awol/no response", "awol"):
            if label in low:
                markers.append(label)
        if markers or sid in active_off:
            exceptions.append({
                "staffId": sid, "name": item["name"],
                "markers": markers,
                "entries": entries,
                "activeTimeOff": active_off.get(sid, [])
            })

    return {
        "date": date,
        "teamId": team_id,
        "counts": {
            "staffWithShiftEvidence": len(by_staff),
            "placementPoolStaff": len(placement),
            "conveyanceCandidatesFromExplicitEvidence": len(conveyance),
            "agencyDriStaff": len(agency),
            "hubStaff": len(hub),
            "absenceTrainingAwolRecords": len(exceptions)
        },
        "placementChecks": placement,
        "conveyanceCandidates": conveyance,
        "agencyDri": agency,
        "hubCover": hub,
        "absenceTrainingAwol": exceptions,
        "rules": {
            "dayOff": "Omitted unless explicitly returned; do not infer pink/blue cell colour.",
            "placement": "Placement Days/Nights 1-8 are pools; other allocation evidence is shown separately.",
            "deletedTimeOff": "Excluded.",
            "colourLimitation": "The documented reports/shifts API does not expose visual per-cell rota colour."
        }
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

@mcp.tool()
async def search_bitrix_chats(query: str):
    """Search recent Bitrix24 dialogs accessible to the configured ESCS user.

    Read-only. Uses im.recent.list, which is compatible with the existing
    user webhook authentication, then filters locally by title/name.
    """
    q = query.strip().lower()
    if len(q) < 2:
        raise ValueError("Chat search requires at least 2 characters")
    team_sync.settings.validate()
    # im.search.chat.list is unavailable to some webhook scopes. The recent
    # dialog list is sufficient for an operational group that the account uses.
    result = await api_call(team_sync.settings, "im.recent.list", {"SKIP_OPENLINES": "Y"})
    rows = result.get("items", result) if isinstance(result, dict) else result
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or row.get("name") or "")
        dialog_id = row.get("id") or row.get("dialogId") or row.get("dialog_id")
        if q not in title.lower():
            continue
        out.append({
            "dialogId": str(dialog_id) if dialog_id is not None else None,
            "name": title,
            "type": row.get("type"),
            "chatId": row.get("chatId") or row.get("chat_id")
        })
    return {"query": query, "matches": out[:20]}

@mcp.tool()
async def send_bitrix_chat_message(dialog_id: str, message: str):
    """Send a plain operational message to an existing Bitrix24 chat.

    Requires an explicit dialog ID (for example chat123). The tool never
    creates chats, adds participants, or guesses a destination.
    """
    if not re.fullmatch(r"(?:chat|sg)\d+", dialog_id):
        raise ValueError("Use an explicit Bitrix dialog ID such as chat123 or sg123")
    body = message.strip()
    if not body:
        raise ValueError("Message cannot be empty")
    if len(body) > 12000:
        raise ValueError("Message is too long")
    team_sync.settings.validate()
    params = {
        "botId": int(team_sync.settings.bot_id),
        "botToken": team_sync.settings.bot_token,
        "dialogId": dialog_id,
        "fields": {"message": body}
    }
    # Keep credentials/URL out of errors while surfacing Bitrix's safe API code.
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        response = await client.post(
            team_sync.settings.rest_url.rstrip("/") + "/imbot.v2.Chat.Message.send",
            json=params
        )
    try:
        payload = response.json()
    except Exception:
        raise RuntimeError(f"Bitrix send failed with HTTP {response.status_code}") from None
    if not response.is_success or "error" in payload:
        code = str(payload.get("error") or f"HTTP_{response.status_code}")[:100]
        desc = str(payload.get("error_description") or "Bitrix rejected request")[:300]
        raise RuntimeError(f"Bitrix send rejected: {code}: {desc}") from None
    result = payload.get("result")
    message_id = result.get("id") if isinstance(result, dict) else result
    return {"sent": True, "dialogId": dialog_id, "messageId": message_id}

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
        async def collect_team_events():
            while True:
                more = False
                if team_sync.settings.enabled and team_sync.settings.event_mode == "fetch":
                    try:
                        more = await team_sync.poll_once()
                    except Exception:
                        # Protected status reports failures; never log payloads.
                        pass
                await asyncio.sleep(1 if more else 15)
        poll_task = asyncio.create_task(collect_team_events())
        async def refresh_daily_lists():
            while True:
                if daily_teams.enabled and team_sync.settings.enabled:
                    try:
                        await daily_teams.run()
                    except Exception:
                        # Protected daily status reports the failure; no roster logs.
                        pass
                await asyncio.sleep(3600)
        daily_task = asyncio.create_task(refresh_daily_lists())
        try:
            yield
        finally:
            retry_task.cancel()
            poll_task.cancel()
            daily_task.cancel()
            try:
                await retry_task
            except asyncio.CancelledError:
                pass
            try:
                await daily_task
            except asyncio.CancelledError:
                pass
            try:
                await poll_task
            except asyncio.CancelledError:
                pass

app = FastAPI(title="ESCS Findmyshift Read-Only Connector", version="0.9.1", lifespan=lifespan)
team_sync = Synchronizer(Settings())
app.include_router(build_router(team_sync, require_connector_key))
daily_teams = DailyTeams(team_sync)
app.include_router(build_daily_router(daily_teams, require_connector_key))

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
