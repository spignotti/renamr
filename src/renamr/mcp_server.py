"""Explicit stdio MCP server exposing only approved contract facts.

There is no HTTP listener, resource surface, SQL/path/fulltext tool, mutation, or
free-form summary. Both the text content and structured response are created
from the same typed whitelist, and a permission change is visible on the next
request because the policy is read from SQLite each time.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server import MCPServer

from renamr import __version__
from renamr.core import mcp_gateway
from renamr.core.db import Database
from renamr.core.paths import CATALOG_FILENAME

INSTRUCTIONS = """\
This server exposes a deliberately small, read-only contract-fact allowlist.

- Discovery shows only safe aliases with at least one local grant.
- Values are normalized dates, numbers, booleans, and bounded enums.
- There is no SQL, filesystem path, full text, excerpt, resource, or mutation tool.
- A cloud-hosted MCP client or model can receive every permitted response. This
  local inference policy does not make those responses private from that client.
- This interface cannot protect against a client that independently has shell or
  filesystem access; such a client can read the same private state directly.
"""


def build_server(state_dir: Path) -> MCPServer:
    """Build the read-only stdio server for one private state directory."""
    database = Database(Path(state_dir) / CATALOG_FILENAME)
    server = MCPServer(
        name="renamr",
        version=__version__,
        instructions=INSTRUCTIONS,
        log_level="ERROR",
    )

    @server.tool(
        name="list_approved_contracts",
        description="List at most 50 locally approved contract aliases.",
        structured_output=True,
    )
    def list_approved_contracts() -> mcp_gateway.ContractDiscovery:
        try:
            return mcp_gateway.discover(database, limit=50)
        except Exception:
            return mcp_gateway.ContractDiscovery(contracts=[], limit=50)

    @server.tool(
        name="get_contract_facts",
        description="Return the granted normalized facts for one approved alias.",
        structured_output=True,
    )
    def get_contract_facts(alias: str) -> mcp_gateway.ContractExport:
        try:
            return mcp_gateway.export(database, alias)
        except Exception:
            return mcp_gateway.ContractExport(available=False, alias="", facts={})

    return server
