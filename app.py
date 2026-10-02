import os
import secrets
from urllib.parse import urlencode
import httpx
from fastapi import FastAPI, HTTPException, Query, Header, Depends
from fastapi.responses import RedirectResponse, JSONResponse
from contextlib import asynccontextmanager

from mcp_server import mcp
mcp_app = mcp.streamable_http_app()

@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield

app = FastAPI(title="ESCS Findmyshift Read-Only Connector", version="0.3.2", lifespan=lifespan)
FMS_BASE="https://www.findmyshift.com"
API_BASE=f"{FMS_BASE}/api/1.4"
AUTH_URL=f"{FMS_BASE}/oauth2-login"
TOKEN_URL=f"{FMS_BASE}/oauth2-token"
CLIENT_ID=os.getenv("FMS_CLIENT_ID","")
CLIENT_SECRET=os.getenv("FMS_CLIENT_SECRET","")
REDIRECT_URI=os.getenv("FMS_REDIRECT_URI","")
CONNECTOR_API_KEY=os.getenv("CONNECTOR_API_KEY","")
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
    if not token: raise HTTPException(401,"Findmyshift is not connected. Visit /oauth/start first.")
    return token

async def fms_get(path,params=None):
    p=dict(params or {}); p["apiKey"]=access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        r=await client.get(f"{API_BASE}/{path}",params=p)
    if r.status_code==429: raise HTTPException(429,"Findmyshift rate limit reached; retry later.")
    if not r.is_success: raise HTTPException(r.status_code,f"Findmyshift API error: {r.text[:500]}")
    try: return r.json()
    except Exception: return {"raw":r.text}

@app.get("/health")
async def health(): return {"ok":True,"connected":bool(TOKENS.get("default",{}).get("access_token"))}

@app.get("/oauth/start")
async def oauth_start():
    require_config(); state=secrets.token_urlsafe(32); OAUTH_STATES.add(state)
    qs=urlencode({"client_id":CLIENT_ID,"redirect_uri":REDIRECT_URI,"response_type":"code","state":state})
    return RedirectResponse(f"{AUTH_URL}?{qs}")

@app.get("/oauth/callback")
async def oauth_callback(code:str,state:str|None=None):
    require_config()
    if state and state not in OAUTH_STATES: raise HTTPException(400,"Invalid OAuth state.")
    if state: OAUTH_STATES.discard(state)
    params={"client_id":CLIENT_ID,"client_secret":CLIENT_SECRET,"code":code,"grant_type":"authorization_code","redirect_uri":REDIRECT_URI}
    async with httpx.AsyncClient(timeout=30) as client: r=await client.post(TOKEN_URL,params=params)
    if not r.is_success: raise HTTPException(r.status_code,f"OAuth token exchange failed: {r.text[:500]}")
    TOKENS["default"]=r.json()
    return JSONResponse({"connected":True,"message":"Findmyshift connected."})

@app.get("/teams",dependencies=[Depends(require_connector_key)])
async def teams(): return await fms_get("teams/list")

@app.get("/staff",dependencies=[Depends(require_connector_key)])
async def staff(teamId:str|None=None): return await fms_get("staff/list",{"teamId":teamId} if teamId else {})

@app.get("/facilities",dependencies=[Depends(require_connector_key)])
async def facilities(teamId:str=Query(...)): return await fms_get("facilities/list",{"teamId":teamId})

@app.get("/shifts",dependencies=[Depends(require_connector_key)])
async def shifts(teamId:str,from_:str=Query(...,alias="from"),to:str=Query(...),publishedShifts:str="yes"):
    return await fms_get("reports/shifts",{"teamId":teamId,"from":from_,"to":to,"publishedShifts":publishedShifts,"comments":"yes","times":"yes","facilities":"yes","groupByStaff":"yes"})

@app.get("/time-off",dependencies=[Depends(require_connector_key)])
async def time_off(teamId:str,from_:str=Query(...,alias="from"),to:str=Query(...)):
    return await fms_get("time-off/list",{"teamId":teamId,"from":from_,"to":to})

@app.get("/primary-check-data",dependencies=[Depends(require_connector_key)])
async def primary_check_data(teamId:str,date:str):
    return {"date":date,"teamId":teamId,"staff":await fms_get("staff/list",{"teamId":teamId}),"facilities":await fms_get("facilities/list",{"teamId":teamId}),"shifts":await fms_get("reports/shifts",{"teamId":teamId,"from":date,"to":date,"publishedShifts":"yes","comments":"yes","times":"yes","facilities":"yes","groupByStaff":"yes"}),"timeOff":await fms_get("time-off/list",{"teamId":teamId,"from":date,"to":date})}


# The MCP ASGI app has its own /mcp protocol route, so mount it at root.
# This preserves the externally visible endpoint as https://escs.onrender.com/mcp.
app.mount("/", mcp_app)
