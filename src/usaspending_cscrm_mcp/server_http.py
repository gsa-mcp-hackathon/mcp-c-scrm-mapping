"""HTTP entrypoint for usaspending_cscrm_mcp (Databricks Apps).

- Binds 0.0.0.0, port from DATABRICKS_APP_PORT / PORT / settings.
- Full v2 toolset via server.create_server() (10 spec-named tools).
- Mounts Streamable HTTP MCP at /mcp.
- GET /health (liveness, no USAspending dependency).
- GET /ready (configuration readiness: auth mode, env presence — no secrets).
- AUTH_MODE=databricks_passthrough: ingress is the trust boundary; no caller
  identity headers are trusted (documented in docs/feasibility_and_auth.md).
  logingov/dual modes fail closed until Login.gov onboarding exists.
"""

from __future__ import annotations

import logging
import os

import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse

from usaspending_cscrm_mcp.server import create_server
from usaspending_cscrm_mcp.settings import Settings

logger = logging.getLogger(__name__)


mcp = create_server()


@mcp.custom_route("/health", methods=["GET"])
async def health_check(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy", "service": "usaspending-cscrm-mcp-v2"})


@mcp.custom_route("/ready", methods=["GET"])
async def readiness_check(_request: Request) -> JSONResponse:
    settings = Settings()
    mode = os.getenv("AUTH_MODE", settings.auth_mode).strip().lower()
    return JSONResponse(
        {
            "ready": True,
            "auth_mode": mode,
            "mcp_path": settings.mcp_path,
            # Presence booleans only — never values. Tells operators whether
            # secret resources resolved, without exposing anything sensitive.
            "signing_key_configured": bool(os.getenv("CSCRM_SIGNING_KEY")),
            "continuation_key_configured": bool(
                os.getenv("CSCRM_CONTINUATION_KEY")),
            "note": "databricks_passthrough: Databricks ingress is the trust boundary; "
            "no caller-supplied identity headers are trusted.",
        }
    )


app = mcp.http_app(path="/mcp")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    port = settings.resolved_port(os.getenv("DATABRICKS_APP_PORT") or os.getenv("PORT"))
    logger.info("Starting %s on %s:%d", settings.app_name, settings.host, port)
    uvicorn.run(
        "usaspending_cscrm_mcp.server_http:app",
        host=settings.host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
