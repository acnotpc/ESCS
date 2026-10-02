import os
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

BASE_URL = os.getenv("CONNECTOR_BASE_URL", "https://escs.onrender.com").rstrip("/")
API_KEY = os.getenv("CONNECTOR_API_KEY", "")

transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["escs.onrender.com", "escs.onrender.com:*"],
    allowed_origins=["https://chatgpt.com", "https://*.chatgpt.com"],
)
mcp = FastMCP("ESCS Findmyshift Primary Checks", transport_security=transport_security)

async def connector_get(path: str, params: dict | None = None):
    if not API_KEY:
        raise RuntimeError("CONNECTOR_API_KEY is not configured")
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.get(
            f"{BASE_URL}{path}",
            params=params or {},
            headers={"X-Connector-Key": API_KEY},
        )
    response.raise_for_status()
    return response.json()

@mcp.tool()
async def list_fms_teams():
    """List Findmyshift teams available to the ESCS account."""
    return await connector_get("/teams")

@mcp.tool()
async def list_fms_staff(team_id: str):
    """List staff for a Findmyshift team."""
    return await connector_get("/staff", {"teamId": team_id})

@mcp.tool()
async def list_fms_facilities(team_id: str):
    """List facilities/placements for a Findmyshift team."""
    return await connector_get("/facilities", {"teamId": team_id})

@mcp.tool()
async def get_fms_shifts(team_id: str, date_from: str, date_to: str):
    """Get published shifts, times, facilities and comments for a date range (YYYY-MM-DD)."""
    return await connector_get("/shifts", {
        "teamId": team_id, "from": date_from, "to": date_to
    })

@mcp.tool()
async def get_fms_time_off(team_id: str, date_from: str, date_to: str):
    """Get Findmyshift time-off records for a date range (YYYY-MM-DD)."""
    return await connector_get("/time-off", {
        "teamId": team_id, "from": date_from, "to": date_to
    })

@mcp.tool()
async def get_primary_check_data(team_id: str, date: str):
    """Get the combined read-only FMS dataset for an ESCS Primary Check on YYYY-MM-DD."""
    return await connector_get("/primary-check-data", {
        "teamId": team_id, "date": date
    })

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
