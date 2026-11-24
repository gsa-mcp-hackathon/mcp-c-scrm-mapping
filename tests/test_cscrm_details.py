"""Tests for the C-SCRM award deep-inspection tool (mocked HTTP)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastmcp import FastMCP
from fastmcp.client import Client

from usa_spending_mcp_server.tools.cscrm_award_details import register_cscrm_award_details

SAMPLE_DETAIL = {
    "generated_unique_award_id": "CONT_AWD_HC102822F0982_9700_HC102817D0001_9700",
    "id": 306490593,
    "piid": "HC102822F0982",
    "type": "DO",
    "type_description": "Delivery Order",
    "category": "contract",
    "description": "MICROSOFT CONSULTING SERVICES",
    "date_signed": "2022-09-30",
    "total_obligation": 219964603.76,
    "base_and_all_options": 219964603.76,
    "recipient": {
        "recipient_name": "MICROSOFT CORPORATION",
        "recipient_uei": "FMVPEWNJGLM1",
        "parent_recipient_name": "MICROSOFT CORPORATION",
        "parent_recipient_uei": "NX52EXRHRDK7",
    },
    "psc_hierarchy": {
        "toptier_code": {"code": "R", "description": "SUPPORT SVCS (PROF, ADMIN, MGMT)"},
        "midtier_code": {"code": "R4", "description": "PROFESSIONAL SERVICES"},
        "subtier_code": {},
        "base_code": {"code": "R425", "description": "SUPPORT- PROFESSIONAL: ENGINEERING/TECHNICAL"},
    },
    "naics_hierarchy": {
        "toptier_code": {"code": "54", "description": "Professional, Scientific, and Technical Services"},
        "midtier_code": {"code": "5415", "description": "Computer Systems Design and Related Services"},
        "base_code": {"code": "541512", "description": "Computer Systems Design Services"},
    },
    "awarding_agency": {
        "id": 1217,
        "has_agency_page": True,
        "toptier_agency": {"name": "Department of Defense", "code": "097", "abbreviation": "DOD"},
        "subtier_agency": {"name": "Defense Information Systems Agency", "code": "97AK", "abbreviation": "DISA"},
        "office_agency_name": "IT CONTRACTING DIVISION - PL83",
    },
    "funding_agency": {
        "id": 1174,
        "has_agency_page": True,
        "toptier_agency": {"name": "Department of Defense", "code": "097", "abbreviation": "DOD"},
        "subtier_agency": {"name": "Department of the Navy", "code": "1700", "abbreviation": "USN"},
        "office_agency_name": "COMMANDER",
    },
    "period_of_performance": {
        "start_date": "2022-09-30",
        "end_date": "2026-03-29",
        "potential_end_date": "2026-09-29 00:00:00",
    },
    "parent_award": {"piid": "HC102817D0001"},
}


@pytest.fixture
async def detail_mcp_client():
    client = AsyncMock()
    client.get.return_value = SAMPLE_DETAIL
    mcp = FastMCP("test-award-details")
    register_cscrm_award_details(mcp, client)
    async with Client(transport=mcp) as c:
        yield c, client


@pytest.mark.asyncio
async def test_returns_parent_recipient_and_hierarchies(detail_mcp_client):
    c, _ = detail_mcp_client
    res = await c.call_tool(
        "get_award_cscrm_details",
        {"generated_internal_id": "CONT_AWD_HC102822F0982_9700_HC102817D0001_9700"},
    )
    payload = res.structured_content
    assert payload["parent_recipient_name"] == "MICROSOFT CORPORATION"
    assert payload["parent_recipient_uei"] == "NX52EXRHRDK7"
    assert payload["psc_levels"][-1]["code"] == "R425"
    assert payload["naics_levels"][-1]["code"] == "541512"


@pytest.mark.asyncio
async def test_splits_awarding_vs_funding_agency(detail_mcp_client):
    c, _ = detail_mcp_client
    res = await c.call_tool(
        "get_award_cscrm_details",
        {"generated_internal_id": "CONT_AWD_HC102822F0982_9700_HC102817D0001_9700"},
    )
    payload = res.structured_content
    assert payload["awarding_agency"]["subtier_name"] == "Defense Information Systems Agency"
    assert payload["funding_agency"]["subtier_name"] == "Department of the Navy"
    assert payload["awarding_agency"]["toptier_code"] == "097"
    assert payload["funding_agency"]["toptier_code"] == "097"


@pytest.mark.asyncio
async def test_rejects_bad_id_without_http_call(detail_mcp_client):
    from fastmcp.exceptions import ToolError

    c, mock_client = detail_mcp_client
    # FastMCP schema validation rejects the bad ID before the tool body runs.
    with pytest.raises(ToolError):
        await c.call_tool("get_award_cscrm_details", {"generated_internal_id": "../../etc"})
    mock_client.get.assert_not_called()
