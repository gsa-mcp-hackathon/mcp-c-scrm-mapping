"""HTTP MCP server entry point for Databricks Apps / cloud.gov."""

from __future__ import annotations

import logging
import os

import uvicorn
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from usa_spending_mcp_server.auth import create_logingov_auth
from usa_spending_mcp_server.client import USASpendingClient
from usa_spending_mcp_server.tools import register_tools

logger = logging.getLogger(__name__)


def _require_auth_env() -> bool:
    return os.getenv("AUTH_ENABLED", "true").strip().lower() in ("1", "true", "yes")


def create_server() -> FastMCP:
    auth = create_logingov_auth()

    require_auth = _require_auth_env()
    if require_auth and not auth:
        raise RuntimeError("Authentication is required in HTTP server mode but no auth was configured")

    client = USASpendingClient(timeout=60.0)

    logger.info("Creating MCP server (auth %s)", "enabled" if auth else "disabled")
    server = FastMCP(
        name="USASpendingCSCRM",
        instructions="MCP server for C-SCRM agency exposure reporting against the live USAspending.gov v2 API.",
        auth=auth if require_auth else None,
    )

    logger.info("Registering tools")
    register_tools(server, client)
    logger.info("Tools registered")

    @server.custom_route("/health", methods=["GET"])
    async def health_check(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "healthy", "service": "usa-spending-cscrm-mcp"})

    return server


mcp = create_server()
app = mcp.http_app()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("DATABRICKS_APP_PORT") or os.getenv("PORT") or "8000")

    logger.info("Starting server on %s:%d", host, port)
    logger.info("Health check: http://%s:%d/health", host, port)

    uvicorn.run(
        "usa_spending_mcp_server.server_http:app",
        host=host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
