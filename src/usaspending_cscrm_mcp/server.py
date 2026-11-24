"""GSA-TTS-style stdio entrypoint: usaspending_cscrm_mcp package + 10 tools."""

from __future__ import annotations

import os

from fastmcp import FastMCP

from usaspending_cscrm_mcp.execution import RunBudgets
from usaspending_cscrm_mcp.settings import Settings
from usaspending_cscrm_mcp.tools import ServerDeps, configure, register_tools


def create_server(deps: ServerDeps | None = None) -> FastMCP:
    if deps is None:
        settings = Settings()
        mode = os.getenv("AUTH_MODE", settings.auth_mode).strip().lower()
        if mode not in ("databricks_passthrough",):
            raise RuntimeError(
                f"AUTH_MODE={mode!r} is not operational: Login.gov onboarding is absent. "
                "Failing closed (no bypass mode permitted).")
        key = os.getenv("CSCRM_CONTINUATION_KEY", "")
        deps = ServerDeps(budgets=RunBudgets(), signing_key=key.encode() or None)
    configure(deps)
    server = FastMCP(
        name="usaspending-cscrm-mcp",
        instructions="C-SCRM agency exposure reporting against the live USAspending.gov v2 API. "
        "Ten tools: target_triage, award_search, agency_exposure_search, award_context, "
        "award_narrative_fetch, vehicle_hierarchy, recipient_graph, lexical_review, "
        "subaward_sweep, audit_run.",
    )
    register_tools(server)
    return server


def main() -> None:
    server = create_server()
    server.run()


if __name__ == "__main__":
    main()
