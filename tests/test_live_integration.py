"""Live integration tests against the real USAspending.gov v2 API.

These hit the network and the live federal API, so they are skipped by
default. Run with::

    CSCRM_LIVE_TESTS=1 python -m pytest tests/test_live_integration.py -v

Live tests use small windows / single-award lookups to stay fast and avoid
hammering the public API.
"""

from __future__ import annotations

import os

import pytest

from usa_spending_mcp_server.client import USASpendingClient
from usa_spending_mcp_server.tools.cscrm_agency_exposure import register_cscrm_tools
from usa_spending_mcp_server.tools.cscrm_award_details import register_cscrm_award_details

pytestmark = pytest.mark.skipif(
    os.getenv("CSCRM_LIVE_TESTS") != "1",
    reason="Live API tests skipped (set CSCRM_LIVE_TESTS=1 to run).",
)

KNOWN_AWARD_ID = "CONT_AWD_HC102822F0982_9700_HC102817D0001_9700"


@pytest.fixture
async def live_client():
    async with USASpendingClient() as client:
        yield client


@pytest.mark.asyncio
async def test_live_discover_vendor_returns_dod(live_client):
    from fastmcp import FastMCP
    from fastmcp.client import Client

    mcp = FastMCP("live-discover")
    register_cscrm_tools(mcp, live_client)
    async with Client(transport=mcp) as c:
        res = await c.call_tool(
            "discover_agencies_by_target",
            {
                "target_query": "Microsoft",
                "search_mode": "vendor",
                "start_date": "2024-10-01",
                "end_date": "2025-09-30",
            },
        )
    payload = res.structured_content
    assert not payload.get("errors"), payload.get("errors")
    assert "Department of Defense" in payload["agencies_using_target"]
    assert payload["counts"]["representative_awards"] > 0
    assert payload["evidence"]["representative_awards"][0]["recipient_uei"]


@pytest.mark.asyncio
async def test_live_award_details_parent_recipient(live_client):
    from fastmcp import FastMCP
    from fastmcp.client import Client

    mcp = FastMCP("live-details")
    register_cscrm_award_details(mcp, live_client)
    async with Client(transport=mcp) as c:
        res = await c.call_tool(
            "get_award_cscrm_details", {"generated_internal_id": KNOWN_AWARD_ID}
        )
    payload = res.structured_content
    assert payload["generated_internal_id"] == KNOWN_AWARD_ID
    assert payload["parent_recipient_name"] == "MICROSOFT CORPORATION"
    assert payload["parent_recipient_uei"] == "NX52EXRHRDK7"
    assert payload["awarding_agency"]["toptier_code"] == "097"
    assert payload["funding_agency"]["toptier_code"] == "097"
    assert payload["naics_levels"][-1]["code"] == "541512"
