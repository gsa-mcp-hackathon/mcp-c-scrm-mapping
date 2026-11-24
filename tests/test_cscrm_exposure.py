"""Unit tests for discover_agencies_by_target (mocked HTTP, no network)."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest
from fastmcp import FastMCP
from fastmcp.client import Client

from usa_spending_mcp_server.tools.cscrm_agency_exposure import (
    _is_award_active,
    register_cscrm_tools,
)

FUTURE = "2099-06-30"
PAST = "2020-01-15"


def _award_row(award_id: str, end_date, uei: str = "FMVPEWNJGLM1") -> dict:
    return {
        "Award ID": award_id,
        "Recipient Name": "MICROSOFT CORPORATION",
        "Awarding Agency": "Department of Defense",
        "Funding Agency": "Department of State",
        "Award Amount": 1000000.0,
        "Description": "test award",
        "End Date": end_date,
        "Recipient UEI": uei,
        "generated_internal_id": f"CONT_AWD_{award_id}_9700_-NONE-_-NONE-",
    }


def _mock_client(award_rows: list[dict] | None = None, fail: bool = False):
    """Mock USASpendingClient capturing POST payloads per endpoint."""
    calls: list[tuple[str, dict]] = []

    async def fake_post(endpoint: str, payload: dict):
        calls.append((endpoint, payload))
        if fail:
            raise RuntimeError("upstream timeout")
        if "spending_by_award" in endpoint and "count" not in endpoint:
            return {"results": award_rows if award_rows is not None else []}
        if "awarding_subagency" in endpoint:
            return {"results": [{"name": "DISA", "agency_name": "Department of Defense", "amount": 10.0}]}
        if "awarding_agency" in endpoint:
            return {"results": [{"name": "Department of Defense", "amount": 100.0}]}
        if "funding_agency" in endpoint:
            return {"results": [{"name": "Department of State", "amount": 75.0}]}
        return {"results": []}

    client = AsyncMock()
    client.post.side_effect = fake_post
    return client, calls


@pytest.fixture
async def exposure_client():
    client, calls = _mock_client([_award_row("A1", FUTURE), _award_row("A2", PAST)])
    mcp = FastMCP("test-exposure")
    register_cscrm_tools(mcp, client)
    async with Client(transport=mcp) as c:
        yield c, client, calls


@pytest.mark.asyncio
async def test_defaults_window_and_contract_types(exposure_client):
    c, _, calls = exposure_client
    await c.call_tool("discover_agencies_by_target", {"target_query": "Microsoft"})
    filters = [p["filters"] for _, p in calls]
    assert filters, "expected upstream POSTs"
    assert all(f["time_period"] == [{"start_date": "2023-10-01", "end_date": "2026-12-31"}] for f in filters)
    assert all(f["award_type_codes"] == ["A", "B", "C", "D"] for f in filters)


@pytest.mark.asyncio
async def test_include_idvs_partitions_vehicles(exposure_client):
    c, _, calls = exposure_client
    await c.call_tool(
        "discover_agencies_by_target", {"target_query": "Microsoft", "include_idvs": True}
    )
    full = ("A", "B", "C", "D", "IDV_A", "IDV_B", "IDV_C", "IDV_D", "IDV_E")
    by_endpoint: dict[str, set] = {}
    for endpoint, payload in calls:
        key = "award" if "spending_by_award" in endpoint else "category"
        by_endpoint.setdefault(key, set()).add(tuple(payload["filters"]["award_type_codes"]))
    # Category endpoints accept the mixed set; award calls are split by
    # homogeneous vehicle group (the API 422s mixed groups).
    assert by_endpoint["category"] == {full}
    assert by_endpoint["award"] == {("A", "B", "C", "D"), ("IDV_A", "IDV_B", "IDV_C", "IDV_D", "IDV_E")}


@pytest.mark.asyncio
async def test_include_idvs_award_results_merge(exposure_client):
    """Split vehicle-group award calls merge into one sorted result set."""
    from unittest.mock import AsyncMock

    async def fake_post(endpoint: str, payload: dict):
        if "spending_by_award" in endpoint:
            codes = payload["filters"]["award_type_codes"]
            if "IDV_A" in codes:
                return {"results": [_award_row("IDV1", FUTURE)]}
            return {"results": [_award_row("C1", FUTURE)]}
        return {"results": []}

    client = AsyncMock()
    client.post.side_effect = fake_post
    mcp = FastMCP("test-merge")
    register_cscrm_tools(mcp, client)
    async with Client(transport=mcp) as c:
        res = await c.call_tool(
            "discover_agencies_by_target", {"target_query": "X", "include_idvs": True}
        )
    awards = res.structured_content["evidence"]["representative_awards"]
    assert sorted(a["award_id"] for a in awards) == ["C1", "C1", "IDV1", "IDV1"]  # one per vector × group
    assert not res.structured_content.get("errors")


@pytest.mark.asyncio
async def test_auto_mode_dual_vector(exposure_client):
    c, _, calls = exposure_client
    res = await c.call_tool(
        "discover_agencies_by_target", {"target_query": "Microsoft", "search_mode": "auto"}
    )
    assert res.structured_content["counts"]["representative_awards"] == 4  # 2 vectors x 2 rows
    filter_keys = {k for _, p in calls for k in p["filters"] if k in ("recipient_search_text", "keywords")}
    assert filter_keys == {"recipient_search_text", "keywords"}


@pytest.mark.asyncio
async def test_active_only_filters_expired_and_null():
    client, _ = _mock_client(
        [_award_row("FUT", FUTURE), _award_row("PAST", PAST), _award_row("NULL", None)]
    )
    mcp = FastMCP("test-active")
    register_cscrm_tools(mcp, client)
    async with Client(transport=mcp) as c:
        res = await c.call_tool(
            "discover_agencies_by_target", {"target_query": "X", "active_only": True}
        )
    awards = res.structured_content["evidence"]["representative_awards"]
    assert [a["award_id"] for a in awards] == ["FUT", "FUT"]  # one per vector


@pytest.mark.asyncio
async def test_tiered_schema_keys(exposure_client):
    c, _, _ = exposure_client
    res = await c.call_tool("discover_agencies_by_target", {"target_query": "Microsoft"})
    payload = res.structured_content
    assert set(payload) >= {
        "agencies_using_target",
        "awarding_agency_names",
        "funding_agency_names",
        "evidence",
        "counts",
    }
    assert set(payload["evidence"]) >= {
        "obligated_amounts",
        "sub_agencies",
        "fulfilling_reseller_ueis",
        "representative_awards",
    }
    assert payload["agencies_using_target"] == sorted(
        set(payload["awarding_agency_names"]) | set(payload["funding_agency_names"])
    )


@pytest.mark.asyncio
async def test_upstream_failure_degrades_gracefully():
    client, _ = _mock_client(fail=True)
    mcp = FastMCP("test-fail")
    register_cscrm_tools(mcp, client)
    async with Client(transport=mcp) as c:
        res = await c.call_tool("discover_agencies_by_target", {"target_query": "Microsoft"})
    payload = res.structured_content
    assert payload["agencies_using_target"] == []
    assert payload["errors"], "expected recorded upstream errors, not a crash"


@pytest.mark.parametrize(
    ("end_date", "expected"),
    [(PAST, False), ("2026-10-23", True), (FUTURE, True), (None, False), ("not-a-date", False)],
)
def test_is_award_active(end_date, expected):
    assert _is_award_active(end_date, today=date(2026, 10, 23)) is expected
