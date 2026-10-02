from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
mcp = FastMCP("ESCS Findmyshift Primary Checks", transport_security=transport_security)

# Tool implementations are attached by app.py so the MCP layer can call the
# same in-process Findmyshift client used by the protected REST endpoints.
