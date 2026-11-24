"""Unit tests for the hardened C-SCRM tools (mocked HTTP, no network)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastmcp import FastMCP
from fastmcp.client import Client

from usa_spending_mcp_server.cscrm_lib import (
    classify_vehicle,
    fpds_subtier_from_gid,
    hhs_component_from_gid,
)
from usa_spending_mcp_server.rate_limit import AsyncTokenBucket, with_backoff
from usa_spending_mcp_server.tools.cscrm_idv import register_traverse_idv_hierarchy
from usa_spending_mcp_server.tools.cscrm_lexicon import adjudicate_candidate
from usa_spending_mcp_server.tools.cscrm_recipient import classify_fulfillment_channel
from usa_spending_mcp_server.tools.cscrm_search import register_search_awards_exhaustive


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_bucket_acquires(self):
        b = AsyncTokenBucket(capacity=5, window_seconds=60)
        await b.acquire(5)
        assert b._tokens <= 5.0

    @pytest.mark.asyncio
    async def test_backoff_retries_then_raises(self):
        from usa_spending_mcp_server.client import USASpendingAPIError

        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            raise USASpendingAPIError("USAspending API request failed (HTTP 503)", status_code=503)

        with pytest.raises(USASpendingAPIError):
            await with_backoff(flaky, retries=1, base_delay=0.01)
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_backoff_ignores_non_retryable(self):
        from usa_spending_mcp_server.client import USASpendingAPIError

        async def bad():
            raise USASpendingAPIError("USAspending API request failed (HTTP 400)", status_code=400)

        with pytest.raises(USASpendingAPIError):
            await with_backoff(bad, retries=3, base_delay=0.01)

    @pytest.mark.asyncio
    async def test_backoff_retries_transport_errors(self):
        import httpx

        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ConnectError("connection reset")
            return "ok"

        assert await with_backoff(flaky, retries=3, base_delay=0.01) == "ok"
        assert calls["n"] == 3


class TestLibHelpers:
    def test_vehicle_classification(self):
        assert classify_vehicle("GS07F190CA") == "BROAD_MULTI_AGENCY_GWAC_MAS"
        assert classify_vehicle("NNG15SC92B") == "BROAD_MULTI_AGENCY_GWAC_MAS"
        assert classify_vehicle("11316023A0004EOP") == "DEDICATED_SINGLE_PURPOSE_IDV"
        assert classify_vehicle(None) == "DEDICATED_SINGLE_PURPOSE_IDV"

    def test_fpds_subtier(self):
        assert fpds_subtier_from_gid("CONT_AWD_X_7524_Y_7524") == "7524"
        assert hhs_component_from_gid("CONT_AWD_X_7524_Y_7524") == "FDA"
        assert hhs_component_from_gid("CONT_AWD_X_7523_Y_7523") == "CDC"
        assert hhs_component_from_gid("CONT_AWD_X_9700_Y_9700") is None


class TestAdjudication:
    def test_tier_1a(self):
        v = adjudicate_candidate("C-CURE 9000 SYSTEM UPGRADE", [], "J063")
        assert v["tier"] == "TIER_1A_CONFIRMED_PURE"

    def test_tier_1b_bundled(self):
        v = adjudicate_candidate("MDI/SOFTWARE HOUSE SECURITY INSTALLATION", [], "N063")
        assert v["tier"] == "TIER_1B_CONFIRMED_BUNDLED_MULTI_OEM"
        assert "mdi" in v["competing_oems"]

    def test_drop_food(self):
        v = adjudicate_candidate("HAM, BONE-IN, F/C, CURED, CHL", [], "8975")
        assert v["tier"] == "DROPPED" and v["rule"] == "DROP_FOOD_RATIONS"

    def test_drop_materials(self):
        v = adjudicate_candidate("180C cure prepregs autoclave run", [], "3210")
        assert v["tier"] == "DROPPED" and v["rule"] == "DROP_AUTOCLAVE_POLYMER"

    def test_drop_isr(self):
        v = adjudicate_candidate("Airborne ISTAR surveillance pod study", [], "5417")
        assert v["tier"] == "DROPPED" and v["rule"] == "DROP_ISR_DOCTRINAL"

    def test_poly_no_context_dropped(self):
        v = adjudicate_candidate("iSTAR holographic display", [], "3345")
        assert v["tier"] == "DROPPED_POLYSEMOUS_NO_SECURITY_CONTEXT"

    def test_poly_with_context_kept(self):
        v = adjudicate_candidate("iSTAR panel upgrade, PACS doors", [], "N063")
        assert v["tier"] == "TIER_1A_CONFIRMED_PURE"

    def test_shi_collision_dropped(self):
        v = adjudicate_candidate("Software House International reseller catalog", [], "5415")
        assert v["rule"] == "DROP_CORPORATE_COLLISION"


class TestFulfillmentChannel:
    def test_oem_subsidiary_by_name(self):
        assert classify_fulfillment_channel("CE8MGXAS9KJ6", None, "JOHNSON CONTROLS, INC") == "OEM_DIRECT_SUBSIDIARY"

    def test_oem_by_parent(self):
        assert classify_fulfillment_channel("X", "K9HJHLAKXE96", "SOME INTEGRATOR") == "OEM_DIRECT_SUBSIDIARY"

    def test_third_party(self):
        assert classify_fulfillment_channel("DT8KJHZXVJH5", "DT8KJHZXVJH5", "CARAHSOFT TECHNOLOGY CORP") == "THIRD_PARTY_INTEGRATOR"


def _award(gid, piid="P1", amt=100.0, desc="C-CURE test"):
    return {"Award ID": piid, "Description": desc, "Award Amount": amt,
            "generated_internal_id": gid, "Recipient Name": "R", "Awarding Agency": "A",
            "Funding Agency": "F", "End Date": "2027-01-01", "Recipient UEI": "U"}


@pytest.fixture
async def search_client():
    client = AsyncMock()

    async def fake_post(endpoint: str, payload: dict):
        if "spending_by_award_count" in endpoint:
            codes = payload["filters"]["award_type_codes"]
            has_c = any(c in codes for c in ("A", "B", "C", "D"))
            has_i = any(c.startswith("IDV") for c in codes)
            return {"results": {"contracts": 2 if has_c else 0, "idvs": 2 if has_i else 0, "other": 0}}
        rows = [_award("G1"), _award("G2")] if "IDV_A" not in payload["filters"]["award_type_codes"] else [_award("IDV1"), _award("IDV2")]
        return {"results": rows, "page_metadata": {"hasNext": False, "page": 1}}

    client.post.side_effect = fake_post
    mcp = FastMCP("test-search")
    register_search_awards_exhaustive(mcp, client)
    async with Client(transport=mcp) as c:
        yield c


@pytest.mark.asyncio
async def test_search_split_and_parity(search_client):
    res = await search_client.call_tool(
        "search_awards_exhaustive",
        {"keywords": ["C-CURE"], "include_idvs": True})
    p = res.structured_content
    assert set(p["groups_run"]) == {"contracts", "idvs"}
    assert p["unique_awards"] == 4
    assert all(v["parity_ok"] for v in p["parity"].values())


@pytest.fixture
async def idv_client():
    client = AsyncMock()

    async def fake_get(endpoint, params=None):
        from usa_spending_mcp_server.client import USASpendingAPIError

        if "BAD_GID" in endpoint:
            raise USASpendingAPIError("USAspending API request failed (HTTP 404)", status_code=404)
        if endpoint.startswith("idvs/amounts"):
            return {"child_award_count": 2, "child_award_total_obligation": 300.0}
        piid = "11316023A0004EOP" if "11316023A0004EOP" in endpoint else "GS07F190CA"
        desc = ("PHYSICAL ACCESS CONTROL SYSTEMS (PACS)" if "11316023A0004EOP" in endpoint
                else "GENERAL SERVICES ADMINISTRATION FEDERAL SUPPLY SCHEDULE")
        return {"piid": piid, "description": desc, "base_and_all_options": 1000.0,
                "total_obligation": 0.0, "type_description": "BPA",
                "period_of_performance": {"start_date": "2022-01-01", "end_date": "2028-06-01",
                                          "potential_end_date": None}}

    async def fake_post(endpoint, payload):
        if payload.get("type") == "child_awards":
            return {"results": [{"generated_unique_award_id": "C1"}, {"generated_unique_award_id": "C2"}],
                    "page_metadata": {"hasNext": False}}
        if "idvs/activity" in endpoint:
            gid = payload.get("award_id", "")
            if "11316023A0004EOP" in gid:
                recips = ["RID_SINGLE_VENDOR", "RID_SINGLE_VENDOR"]
            else:
                recips = ["RID_VENDOR_A", "RID_VENDOR_B"]
            return {"results": [{"recipient_id": r, "recipient_name": f"VENDOR {r}"}
                                for r in recips],
                    "page_metadata": {"hasNext": False}}
        return {"results": [], "page_metadata": {"hasNext": False}}

    client.get.side_effect = fake_get
    client.post.side_effect = fake_post
    mcp = FastMCP("test-idv")
    register_traverse_idv_hierarchy(mcp, client)
    async with Client(transport=mcp) as c:
        yield c


@pytest.mark.asyncio
async def test_traverse_parity_and_burn(idv_client):
    res = await idv_client.call_tool(
        "traverse_idv_hierarchy", {"parent_gids": ["CONT_IDV_11316023A0004EOP_1100"]})
    v = res.structured_content["vehicles"][0]
    assert v["classification"] == "DEDICATED_SINGLE_PURPOSE_IDV"
    assert v["parity_ok"] is True and v["child_count"] == 2
    assert v["ceiling_burn_pct"] == 0.3
    assert v["recompete_flag"] is False


@pytest.mark.asyncio
async def test_broad_vehicle_skipped(idv_client):
    res = await idv_client.call_tool(
        "traverse_idv_hierarchy", {"parent_gids": ["CONT_IDV_GS07F190CA_4732"]})
    v = res.structured_content["vehicles"][0]
    assert v["classification"] == "BROAD_MULTI_PURPOSE_IDV"
    assert v["traversal"] == "skipped-broad-vehicle"


@pytest.mark.asyncio
async def test_unresolvable_vehicle_does_not_abort_siblings(idv_client):
    res = await idv_client.call_tool(
        "traverse_idv_hierarchy",
        {"parent_gids": ["CONT_IDV_11316023A0004EOP_1100", "CONT_IDV_BAD_GID_0000"]})
    vs = {v["parent_gid"]: v for v in res.structured_content["vehicles"]}
    assert vs["CONT_IDV_11316023A0004EOP_1100"]["parity_ok"] is True
    assert vs["CONT_IDV_BAD_GID_0000"]["classification"] == "UNRESOLVABLE"
    assert vs["CONT_IDV_BAD_GID_0000"]["traversal"] == "skipped-error"


@pytest.mark.asyncio
async def test_get_endpoints_use_trailing_slash():
    """Regression: the API 301s slashless GETs; all GETs must end with '/'."""
    from unittest.mock import AsyncMock

    from usa_spending_mcp_server.tools.cscrm_context import fetch_award_full_context_core
    from usa_spending_mcp_server.tools.cscrm_idv import traverse_idv_core

    seen: list[str] = []
    client = AsyncMock()

    async def fake_get(endpoint, params=None):
        seen.append(endpoint)
        if endpoint.startswith("idvs/amounts"):
            return {"child_award_count": 0, "child_award_total_obligation": 0.0}
        return {"piid": "P1", "base_and_all_options": 100.0, "total_obligation": 0.0,
                "type_description": "BPA", "period_of_performance": {},
                "recipient": {}, "psc_hierarchy": {}, "naics_hierarchy": {}}

    async def fake_post(endpoint, payload):
        return {"results": [], "page_metadata": {"hasNext": False}}

    client.get.side_effect = fake_get
    client.post.side_effect = fake_post
    await fetch_award_full_context_core(client, ["CONT_AWD_X_9700_-NONE-_-NONE-"])
    await traverse_idv_core(client, ["CONT_IDV_X_9700"])
    get_endpoints = [e for e in seen if e.startswith(("awards/", "idvs/", "recipient/"))]
    assert get_endpoints, "expected GET calls"
    assert all(e.endswith("/") for e in get_endpoints), get_endpoints


@pytest.mark.asyncio
async def test_subaward_sweep_sends_required_codes_and_subaward_shape():
    """Regression: subawards:true keyword sweep must carry award_type_codes
    (422 without) plus subaward fields/sort (prime fields 422 under subawards)."""
    from usa_spending_mcp_server.tools.cscrm_subawards import (
        register_search_subawards_exhaustive,
    )

    seen: list[dict] = []
    client = AsyncMock()

    async def fake_post(endpoint, payload):
        seen.append({"endpoint": endpoint, "payload": payload})
        return {"results": [], "page_metadata": {"hasNext": False}}

    client.post.side_effect = fake_post
    mcp = FastMCP("test-subawards")
    register_search_subawards_exhaustive(mcp, client)
    async with Client(transport=mcp) as c:
        res = await c.call_tool("search_subawards_exhaustive",
                                {"prime_gids": ["G1"], "keywords": ["C-CURE"]})
    sweeps = [s for s in seen if s["payload"].get("subawards") is True]
    assert sweeps, "expected a subawards:true sweep call"
    sw = sweeps[0]["payload"]
    assert sw["filters"].get("award_type_codes"), "award_type_codes required"
    assert sw["sort"] == "Sub-Award Amount", sw["sort"]
    assert "Sub-Award ID" in sw["fields"] and "Award Amount" not in sw["fields"]
    body = res.structured_content
    assert body is not None
    assert "tier2_subaward_ledger" in body and "keyword_subaward_hits" in body


@pytest.mark.asyncio
async def test_triage_nonsecurity_domain_and_short_stem():
    """Stage 0 generalizes: searched phrase counts as positive evidence in any
    domain; short stems cap at CONTEXT_GATED."""
    from usa_spending_mcp_server.tools.cscrm_triage import triage_and_deconflict_target_core

    client = AsyncMock()

    async def fake_post(endpoint, payload):
        if "spending_by_award_count" in endpoint:
            return {"results": {"contracts": 3, "other": 0}}
        kw = payload["filters"]["keywords"]
        if kw == ["ACL"]:
            rows = [{"Award ID": f"A{i}", "Recipient Name": "HHS",
                     "Description": f"ACL DATA ANALYTICS PLATFORM SUPPORT {i}",
                     "Award Amount": 1.0, "Awarding Agency": "A",
                     "generated_internal_id": f"A{i}"} for i in range(5)]
        else:
            rows = [{"Award ID": "P1", "Recipient Name": "RESELLER INC",
                     "Description": "ACL ANALYTICS ENTERPRISE LICENSES RENEWAL",
                     "Award Amount": 1.0, "Awarding Agency": "A",
                     "generated_internal_id": "P1"}]
        return {"results": rows, "page_metadata": {"hasNext": False}}

    client.post.side_effect = fake_post
    client.get.side_effect = AsyncMock(return_value=[])
    out = await triage_and_deconflict_target_core(
        client, "ACL Analytics", candidate_terms=["ACL Analytics", "ACL"],
        confirmed_scope={"scope_choice": "product-line-only",
                         "is_pure_play_vendor": False})
    man = out["manifest"] if out["status"] == "READY" else out["draft_manifest"]
    assert "ACL Analytics" in man["standalone_keywords"]
    assert "ACL" in man["context_gated_keywords"]
    assert "ACL" not in man["standalone_keywords"]


@pytest.mark.asyncio
async def test_triage_precision_and_manifest():
    """Stage 0: overbroad term rejected, safe term standalone, manifest locked."""
    from usa_spending_mcp_server.tools.cscrm_triage import triage_and_deconflict_target_core

    client = AsyncMock()

    async def fake_post(endpoint, payload):
        if "spending_by_award_count" in endpoint:
            return {"results": {"contracts": 10, "other": 0}}
        kw = payload["filters"]["keywords"]
        if kw == ["CURE"]:
            rows = [{"Award ID": f"P{i}", "Recipient Name": "FOOD CO",
                     "Description": "cured ham processing line",
                     "Award Amount": 1.0, "Awarding Agency": "A",
                     "generated_internal_id": f"G{i}"} for i in range(25)]
        else:
            rows = [{"Award ID": f"C{i}", "Recipient Name": "SECURE INC",
                     "Description": "C-CURE 9000 access control install",
                     "Award Amount": 1.0, "Awarding Agency": "A",
                     "generated_internal_id": f"C{i}"} for i in range(25)]
        return {"results": rows, "page_metadata": {"hasNext": False}}

    async def fake_get(endpoint, params=None):
        return []

    client.post.side_effect = fake_post
    client.get.side_effect = fake_get
    out = await triage_and_deconflict_target_core(
        client, "C-CURE 9000", candidate_terms=["C-CURE 9000", "CURE"],
        confirmed_scope={"scope_choice": "product-line-only",
                         "oem_parent_name": "Johnson Controls"})
    assert out["status"] == "READY"
    man = out["manifest"]
    assert "C-CURE 9000" in man["standalone_keywords"]
    assert "CURE" in man["rejected_terms"]
    assert man["target_archetype"] == "ARCHETYPE_2_CONGLOMERATE_PRODUCT_LINE"
    assert man["artifact"] == "cscrm_stage0_manifest.json"


@pytest.mark.asyncio
async def test_triage_needs_input_on_ambiguity():
    """Stage 0: conglomerate scope without confirmation pauses with questions."""
    from usa_spending_mcp_server.tools.cscrm_triage import triage_and_deconflict_target_core

    client = AsyncMock()

    async def fake_post(endpoint, payload):
        if "spending_by_award_count" in endpoint:
            return {"results": {"contracts": 5, "other": 0}}
        rows = [{"Award ID": f"C{i}", "Recipient Name": "SECURE INC",
                 "Description": "C-CURE 9000 install", "Award Amount": 1.0,
                 "Awarding Agency": "A", "generated_internal_id": f"C{i}"}
                for i in range(5)]
        return {"results": rows, "page_metadata": {"hasNext": False}}

    client.post.side_effect = fake_post
    client.get.side_effect = AsyncMock(return_value=[])
    out = await triage_and_deconflict_target_core(client, "Johnson Controls")
    assert out["status"] == "NEEDS_USER_INPUT"
    assert any(t.startswith("A_") for t in out["triggers"])
    assert out["questions"], "expected targeted clarification questions"


@pytest.mark.asyncio
async def test_fix1_keyword_matched_broad_child_retained():
    """Fix 1 (strict): a keyword-confirmed award under a BROAD parent is
    RETAINED as Tier-1 evidence (only its own GID-attributed transactions
    count). Quarantine applies solely to non-keyword-matched broad children.
    The mirage dies via the DEDICATED gate, not by suppressing evidence."""
    from usa_spending_mcp_server.tools.cscrm_audit import run_audit_core

    client = AsyncMock()
    gid_broad = "CONT_AWD_BIG_9700_GS07F190CA_4732"
    gid_pure = "CONT_AWD_PURE_3600_-NONE-_-NONE-"

    async def fake_post(endpoint, payload):
        if "spending_by_award_count" in endpoint:
            return {"results": {"contracts": 2, "idvs": 0, "other": 0}}
        if payload.get("subawards") is True:
            return {"results": [], "page_metadata": {"hasNext": False}}
        if "idvs/awards" in endpoint or "idvs/activity" in endpoint:
            return {"results": [], "page_metadata": {"hasNext": False}}
        if "awards/funding" in endpoint:
            return {"results": []}
        if "search/spending_by_transaction" in endpoint:
            gid = (payload.get("filters", {}).get("keywords") or [""])[0]
            amt = 1000.0 if gid == "BIG" else 100.0
            return {"results": [{"Transaction Amount": amt, "Action Date": "2024-05-01",
                                 "Funding Agency": "Dept of Defense" if gid == "BIG" else "VA",
                                 "Awarding Agency": "Dept of Defense" if gid == "BIG" else "VA",
                                 "generated_internal_id": gid_broad if gid == "BIG" else gid_pure}],
                    "page_metadata": {"hasNext": False}}
        if "search/spending_by_award" in endpoint:
            return {"results": [
                {"Award ID": "BIG", "Description": "C-CURE 9000 call",
                 "Award Amount": 1000.0, "generated_internal_id": gid_broad,
                 "Recipient Name": "R", "Awarding Agency": "Dept of Defense",
                 "Funding Agency": "Dept of Defense", "End Date": "2027-01-01",
                 "Recipient UEI": "U"},
                {"Award ID": "PURE", "Description": "C-CURE 9000 install",
                 "Award Amount": 100.0, "generated_internal_id": gid_pure,
                 "Recipient Name": "R", "Awarding Agency": "VA",
                 "Funding Agency": "VA", "End Date": "2027-01-01",
                 "Recipient UEI": "U"}],
                "page_metadata": {"hasNext": False}}
        return {"results": [], "page_metadata": {"hasNext": False}}

    async def fake_get(endpoint, params=None):
        if endpoint.startswith("idvs/amounts"):
            return {"child_award_count": 0, "child_award_total_obligation": 0.0}
        if endpoint.startswith("awards/CONT_IDV_GS07F190CA"):
            return {"piid": "GS07F190CA", "total_obligation": 797000000.0,
                    "base_and_all_options": 797000000.0, "type_description": "BPA",
                    "description": "GSA FEDERAL SUPPLY SCHEDULE CONTRACT GS07F190CA",
                    "parent_award": {"generated_unique_award_id": "CONT_IDV_GS07F190CA_4732",
                                     "piid": "GS07F190CA"},
                    "recipient": {"recipient_name": "R", "recipient_uei": "U"},
                    "awarding_agency": {}, "funding_agency": {},
                    "period_of_performance": {}, "psc_hierarchy": {},
                    "naics_hierarchy": {"base_code": {"code": "541512"}}}
        if "GS07F190CA" in endpoint:
            return {"piid": "BIG", "total_obligation": 797000000.0,
                    "base_and_all_options": 797000000.0, "type_description": "Delivery Order",
                    "description": "C-CURE 9000 call off GSA schedule vehicle",
                    "parent_award": {"generated_unique_award_id": "CONT_IDV_GS07F190CA_4732",
                                     "piid": "GS07F190CA"},
                    "recipient": {"recipient_name": "R", "recipient_uei": "U"},
                    "awarding_agency": {}, "funding_agency": {},
                    "period_of_performance": {"start_date": "2024-01-01", "end_date": "2027-01-01"},
                    "psc_hierarchy": {"base_code": {"code": "J063"}},
                    "naics_hierarchy": {"base_code": {"code": "541512"}}}
        if endpoint.startswith("recipient/children"):
            return []
        return {"piid": "PURE", "total_obligation": 100.0,
                "base_and_all_options": 100.0, "type_description": "Contract",
                "description": "C-CURE 9000 install",
                "parent_award": {},
                "recipient": {"recipient_name": "R", "recipient_uei": "U"},
                "awarding_agency": {}, "funding_agency": {},
                "period_of_performance": {"start_date": "2024-01-01", "end_date": "2027-01-01"},
                "psc_hierarchy": {"base_code": {"code": "J063"}},
                "naics_hierarchy": {"base_code": {"code": "541512"}}}

    client.post.side_effect = fake_post
    client.get.side_effect = fake_get
    cert = await run_audit_core(client, ["C-CURE 9000"], "2023-10-01", "2026-12-31",
                                assumed_idvs=["CONT_IDV_GS07F190CA_4732"])
    assert gid_broad not in cert["quarantined_tier3_broad_vehicle"]
    assert cert["quarantined_tier3_in_window_usd"] == 0.0
    dod = next((m for m in cert["matrix"] if m["agency"] == "Dept of Defense"), None)
    assert dod is not None and dod["funded"] == 1000.0 and dod["tier1a_in_window"] == 1000.0


@pytest.mark.asyncio
async def test_fix2_division_dba_vector_runs_instead_of_parent():
    """Fix 2.1: conglomerate + division_names runs recipient search on the DBA only."""
    from fastmcp import FastMCP
    from fastmcp.client import Client as MCPClient

    from usa_spending_mcp_server.tools.cscrm_agency_exposure import register_cscrm_tools

    client = AsyncMock()

    async def fake_post(endpoint, payload):
        return {"results": [], "page_metadata": {"hasNext": False}}

    client.post.side_effect = fake_post
    mcp = FastMCP("test-fix21")
    register_cscrm_tools(mcp, client)
    async with MCPClient(transport=mcp) as c:
        res = await c.call_tool("discover_agencies_by_target",
                                {"target_query": "Johnson Controls",
                                 "search_mode": "vendor",
                                 "target_archetype": "conglomerate_product",
                                 "division_names": ["Software House"]})
    body = res.structured_content
    assert body is not None
    assert "Software House" in body["recipient_vector_status"]
    sent = [call.args[1]["filters"] for call in client.post.call_args_list]
    recp = [f for f in sent if "recipient_search_text" in f]
    assert recp, "expected division DBA recipient searches"
    assert all(f["recipient_search_text"] == ["Software House"] for f in recp)


@pytest.mark.asyncio
async def test_fix2_conglomerate_recipient_vector_skipped():
    """Fix 2: conglomerate archetype skips recipient_search_text unless allowed."""
    from fastmcp import FastMCP
    from fastmcp.client import Client as MCPClient

    from usa_spending_mcp_server.tools.cscrm_agency_exposure import register_cscrm_tools

    client = AsyncMock()

    async def fake_post(endpoint, payload):
        return {"results": [], "page_metadata": {"hasNext": False}}

    client.post.side_effect = fake_post
    mcp = FastMCP("test-fix2")
    register_cscrm_tools(mcp, client)
    async with MCPClient(transport=mcp) as c:
        res = await c.call_tool("discover_agencies_by_target",
                                {"target_query": "Johnson Controls",
                                 "search_mode": "auto",
                                 "target_archetype": "conglomerate_product"})
    body = res.structured_content
    assert body is not None
    assert body["recipient_vector_status"].startswith("recipient_search_text vector skipped")
    sent_filters = [call.args[1].get("filters", {}) for call in client.post.call_args_list]
    assert not any("recipient_search_text" in f for f in sent_filters)


@pytest.mark.asyncio
async def test_fix3_ghost_rows_excluded_and_deobligation_flagged():
    """Fix 3: $0 rows excluded from in-window sums; net-negative flagged."""
    from usa_spending_mcp_server.tools.cscrm_context import fetch_award_full_context_core

    client = AsyncMock()

    async def fake_post(endpoint, payload):
        if "search/spending_by_transaction" in endpoint:
            return {"results": [
                {"Transaction Amount": 0.0, "Action Date": "2024-03-01",
                 "generated_internal_id": "G1"},
                {"Transaction Amount": 500.0, "Action Date": "2024-04-01",
                 "generated_internal_id": "G1"},
                {"Transaction Amount": -700.0, "Action Date": "2024-05-01",
                 "generated_internal_id": "G1"}],
                "page_metadata": {"hasNext": False}}
        return {"results": [], "page_metadata": {"hasNext": False}}

    async def fake_get(endpoint, params=None):
        return {"piid": "P1", "total_obligation": -200.0,
                "base_and_all_options": 1000.0, "type_description": "Contract",
                "description": "C-CURE 9000", "parent_award": {},
                "recipient": {"recipient_name": "R", "recipient_uei": "U"},
                "awarding_agency": {}, "funding_agency": {},
                "period_of_performance": {"start_date": "2024-01-01", "end_date": "2027-01-01"},
                "psc_hierarchy": {"base_code": {"code": "J063"}},
                "naics_hierarchy": {"base_code": {"code": "541512"}}}

    client.post.side_effect = fake_post
    client.get.side_effect = fake_get
    out = await fetch_award_full_context_core(client, ["G1"], "2023-10-01", "2026-12-31")
    h = out["awards"][0]
    assert h["zero_amount_rows_excluded"] == 1
    assert h["in_window_obligated"] == -200.0
    assert h["deobligated_flag"] is True


@pytest.mark.asyncio
async def test_fix4_retry_after_and_jitter():
    """Fix 4: Retry-After honored; 429 triggers the 60s cooldown; 503 retries fast."""
    from unittest.mock import patch

    from usa_spending_mcp_server import rate_limit as rl
    from usa_spending_mcp_server.client import USASpendingAPIError
    from usa_spending_mcp_server.rate_limit import with_backoff

    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    async def flaky_429():
        raise USASpendingAPIError("USAspending API request failed (HTTP 429)",
                                  status_code=429, retry_after=0.01)

    with patch.object(rl.asyncio, "sleep", fake_sleep):
        try:
            await with_backoff(flaky_429, retries=1, base_delay=0.01, max_delay=0.05)
            raise AssertionError("should have raised")
        except USASpendingAPIError:
            pass
    assert sleeps and sleeps[0] >= 60.0

    calls = {"n": 0}

    async def flaky_503():
        calls["n"] += 1
        if calls["n"] < 3:
            raise USASpendingAPIError("USAspending API request failed (HTTP 503)",
                                      status_code=503)
        return "ok"

    assert await with_backoff(flaky_503, retries=4, base_delay=0.01, max_delay=0.05) == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_fix4_wrapped_transport_errors_retry():
    """Root-cause regression: USASpendingAPIError(transient=True) must retry.

    The client wraps httpx transport failures (RemoteProtocolError RSTs,
    timeouts) in USASpendingAPIError; without the transient flag with_backoff
    treated them as fatal and the subaward sweep died in ~1s with no retry.
    """
    import httpx

    from usa_spending_mcp_server.client import USASpendingAPIError
    from usa_spending_mcp_server.rate_limit import with_backoff

    calls = {"n": 0}

    async def rst_then_ok():
        calls["n"] += 1
        if calls["n"] < 3:
            raise USASpendingAPIError(
                "USAspending API request failed (RemoteProtocolError)",
                transient=True) from httpx.RemoteProtocolError(
                    "peer closed connection without sending complete message",
                    request=None)
        return "recovered"

    assert await with_backoff(rst_then_ok, retries=4,
                              base_delay=0.01, max_delay=0.05) == "recovered"
    assert calls["n"] == 3

    fatal = {"n": 0}

    async def http400():
        fatal["n"] += 1
        raise USASpendingAPIError("USAspending API request failed (HTTP 400)",
                                  status_code=400)

    try:
        await with_backoff(http400, retries=3, base_delay=0.01, max_delay=0.02)
        raise AssertionError("should have raised")
    except USASpendingAPIError:
        pass
    assert fatal["n"] == 1


def _traverse_client(detail_desc, child_descs, amounts_count=4):
    """Mock client: parent description + N children with given descriptions."""
    from usa_spending_mcp_server.tools.cscrm_idv import traverse_idv_core  # noqa: F401

    client = AsyncMock()

    async def fake_get(endpoint, params=None):
        if endpoint.startswith("idvs/amounts"):
            return {"child_award_count": amounts_count,
                    "child_award_total_obligation": 400.0}
        return {"piid": "TESTPARENT", "description": detail_desc,
                "base_and_all_options": 1000.0, "total_obligation": 0.0,
                "type_description": "BPA",
                "period_of_performance": {"start_date": "2022-01-01", "end_date": "2028-06-01",
                                          "potential_end_date": None}}

    async def fake_post(endpoint, payload):
        if "idvs/awards" in endpoint:
            rows = [{"generated_unique_award_id": f"KID{i}",
                     "description": d} for i, d in enumerate(child_descs)]
            return {"results": rows, "page_metadata": {"hasNext": False}}
        if "idvs/activity" in endpoint:
            return {"results": [{"recipient_id": "RID_SINGLE", "recipient_name": "SOLE VENDOR"}
                                for _ in child_descs],
                    "page_metadata": {"hasNext": False}}
        return {"results": [], "page_metadata": {"hasNext": False}}

    client.get.side_effect = fake_get
    client.post.side_effect = fake_post
    return client


@pytest.mark.asyncio
async def test_manifest_adjudication_generalizes_beyond_pacs():
    """Manifest standalone literals confer TIER_1 without PACS patterns."""
    from usa_spending_mcp_server.tools.cscrm_lexicon import adjudicate_candidate

    man = {"standalone_keywords": ["ACL Analytics"],
           "positive_domain_context_regex": "\\b(audit|analytics)\\b"}
    v = adjudicate_candidate("ACL ANALYTICS ENTERPRISE LICENSES RENEWAL",
                             ["ACL ANALYTICS ENTERPRISE LICENSES"], None,
                             manifest=man)
    assert v["tier"] == "TIER_1A_CONFIRMED_PURE"
    assert v["rule"] == "KEEP_MANIFEST_PRODUCT_MATCH"
    v2 = adjudicate_candidate("GALVANIZED PIPE REPLACEMENT", [], None, manifest=man)
    assert v2["tier"] == "DROPPED_POLYSEMOUS_NO_SECURITY_CONTEXT"


@pytest.mark.asyncio
async def test_gate_b_density_admits_generic_parent_with_matching_children():
    """Gate B: generic parent + >=20% keyword children + <=50 -> DEDICATED."""
    from usa_spending_mcp_server.tools.cscrm_idv import traverse_idv_core

    kids = (["C-CURE 9000 software support agreement"] * 3
            + ["GENERAL IT SERVICES"] * 7)
    client = _traverse_client("BLANKET PURCHASE AGREEMENT VEHICLE", kids)
    out = await traverse_idv_core(client, ["CONT_IDV_X_0000"],
                                  keep_keywords=["C-CURE 9000"])
    v = out["vehicles"][0]
    assert v["classification"] == "DEDICATED_SINGLE_PURPOSE_IDV"
    assert v["classification_rule"] == "gate-B-density"
    assert v["keyword_density"] == 0.3


@pytest.mark.asyncio
async def test_gate_broad_by_size_and_low_density():
    """>50 children -> BROAD even with matching parent; low density -> BROAD."""
    from usa_spending_mcp_server.tools.cscrm_idv import traverse_idv_core

    huge = (["PHYSICAL ACCESS CONTROL SYSTEMS (PACS) upgrade"] * 51
            + ["UNRELATED"] * 10)
    client = _traverse_client("PHYSICAL ACCESS CONTROL SYSTEMS (PACS)", huge)
    v = (await traverse_idv_core(client, ["CONT_IDV_BIG_0000"]))["vehicles"][0]
    assert v["classification"] == "BROAD_MULTI_PURPOSE_IDV"
    assert v["classification_rule"] == "broad-exceeds-50-children"
    assert v["children"] == []

    sparse = ["C-CURE 9000 support"] + ["UNRELATED SUPPLIES"] * 9
    client2 = _traverse_client("MISCELLANEOUS PROCUREMENT VEHICLE", sparse)
    v2 = (await traverse_idv_core(client2, ["CONT_IDV_SPARSE_0000"]))["vehicles"][0]
    assert v2["classification"] == "BROAD_MULTI_PURPOSE_IDV"
    assert v2["keyword_matched_children"] == 1


@pytest.mark.asyncio
async def test_ghost_tier_and_historical_ledger():
    """Fix 3 strict: ended $0 awards -> HISTORICAL, out of matrix, in ledger."""
    from usa_spending_mcp_server.tools.cscrm_audit import run_audit_core

    client = AsyncMock()
    gid_live = "CONT_AWD_LIVE_3600_-NONE-_-NONE-"
    gid_ghost = "CONT_AWD_GHOST_3600_-NONE-_-NONE-"

    async def fake_post(endpoint, payload):
        if "spending_by_award_count" in endpoint:
            return {"results": {"contracts": 2, "idvs": 0, "other": 0}}
        if payload.get("subawards") is True:
            return {"results": [], "page_metadata": {"hasNext": False}}
        if "search/spending_by_transaction" in endpoint:
            return {"results": [{
                "Transaction Amount": 1000.0, "Action Date": "2024-06-01",
                "Action Type": "A", "generated_internal_id": gid_live,
                "Funding Agency": "VA", "Awarding Agency": "VA",
                "Transaction Description": "C-CURE 9000 install"}],
                "page_metadata": {"hasNext": False}}
        if "search/spending_by_award" in endpoint:
            def mk(g, d):
                return {"Award ID": g, "Description": d, "Award Amount": 1.0,
                        "generated_internal_id": g, "Recipient Name": "R",
                        "Awarding Agency": "VA", "Funding Agency": "VA",
                        "End Date": "2027-01-01", "Recipient UEI": "U"}
            return {"results": [mk(gid_live, "C-CURE 9000 install"),
                                mk(gid_ghost, "C-CURE 9000 legacy")],
                    "page_metadata": {"hasNext": False}}
        return {"results": [], "page_metadata": {"hasNext": False}}

    async def fake_get(endpoint, params=None):
        if endpoint.startswith("recipient/children"):
            return []
        live = endpoint.endswith(f"{gid_live}/")
        return {"piid": "LIVE" if live else "GHOST",
                "total_obligation": 1000.0 if live else 0.0,
                "base_and_all_options": 1000.0, "type_description": "Contract",
                "description": "C-CURE 9000 install" if live else "C-CURE 9000 legacy",
                "parent_award": {},
                "recipient": {"recipient_name": "R", "recipient_uei": "U"},
                "awarding_agency": {}, "funding_agency": {},
                "period_of_performance": {"start_date": "2024-01-01",
                                          "end_date": "2027-01-01" if live else "2024-01-01"},
                "psc_hierarchy": {"base_code": {"code": "J063"}},
                "naics_hierarchy": {"base_code": {"code": "541512"}}}

    client.post.side_effect = fake_post
    client.get.side_effect = fake_get
    cert = await run_audit_core(client, ["C-CURE 9000"], "2023-10-01", "2026-12-31")
    assert gid_ghost in cert["historical_gids"]
    assert any(r["gid"] == gid_ghost for r in cert["historical_deobligated"])
    va = next(m for m in cert["matrix"] if m["agency"] == "VA")
    assert va["tier1a_in_window"] == 1000.0
    assert va["usage_confidence"] == "HIGH"


@pytest.mark.asyncio
async def test_at00_manifest_gate():
    """AT-00: missing manifest FAILs; locked manifest with precisions PASSes."""
    from usa_spending_mcp_server.tools.cscrm_audit import run_audit_core

    client = AsyncMock()

    async def fake_post(endpoint, payload):
        if "spending_by_award_count" in endpoint:
            return {"results": {"contracts": 0, "idvs": 0, "other": 0}}
        return {"results": [], "page_metadata": {"hasNext": False}}

    client.post.side_effect = fake_post
    client.get.side_effect = AsyncMock(return_value={})
    no_man = await run_audit_core(client, ["X"], "2023-10-01", "2026-12-31")
    assert no_man["checks"]["AT-00"]["status"] == "FAIL"
    man = {"is_pure_play_vendor": False, "standalone_keywords": ["C-CURE 9000"],
           "context_gated_keywords": ["Software House"],
           "positive_domain_context_regex": "\\bpacs\\b",
           "negative_drop_regex": "\\bham\\b",
           "probe_table": [{"term": "C-CURE 9000", "sample_precision": 1.0,
                            "disposition": "STANDALONE_SAFE"}]}
    yes_man = await run_audit_core(client, ["X"], "2023-10-01", "2026-12-31",
                                   manifest=man)
    assert yes_man["checks"]["AT-00"]["status"] == "PASS"


@pytest.mark.asyncio
async def test_at04_conglomerate_product_evidence():
    """AT-04: non-pure-play award kept without product text or lineage FAILs."""
    from usa_spending_mcp_server.tools.cscrm_audit import run_audit_core

    client = AsyncMock()
    gid = "CONT_AWD_SO_3600_-NONE-_-NONE-"

    async def fake_post(endpoint, payload):
        if "spending_by_award_count" in endpoint:
            return {"results": {"contracts": 1, "idvs": 0, "other": 0}}
        if payload.get("subawards") is True:
            return {"results": [], "page_metadata": {"hasNext": False}}
        if "search/spending_by_transaction" in endpoint:
            return {"results": [{
                "Transaction Amount": 50.0, "Action Date": "2024-06-01",
                "Action Type": "A", "generated_internal_id": gid,
                "Funding Agency": "VA", "Awarding Agency": "VA",
                "Transaction Description": "SOFTWARE HOUSE panel upgrade"}],
                "page_metadata": {"hasNext": False}}
        if "search/spending_by_award" in endpoint:
            return {"results": [{"Award ID": "SO", "Description": "SOFTWARE HOUSE panel upgrade",
                                 "Award Amount": 50.0, "generated_internal_id": gid,
                                 "Recipient Name": "JOHNSON CONTROLS INC",
                                 "Awarding Agency": "VA", "Funding Agency": "VA",
                                 "End Date": "2027-01-01", "Recipient UEI": "CE8MGXAS9KJ6"}],
                    "page_metadata": {"hasNext": False}}
        return {"results": [], "page_metadata": {"hasNext": False}}

    async def fake_get(endpoint, params=None):
        if endpoint.startswith("recipient/children"):
            return []
        return {"piid": "SO", "total_obligation": 50.0,
                "base_and_all_options": 50.0, "type_description": "Contract",
                "description": "SOFTWARE HOUSE panel upgrade", "parent_award": {},
                "recipient": {"recipient_name": "JOHNSON CONTROLS INC",
                              "recipient_uei": "CE8MGXAS9KJ6"},
                "awarding_agency": {}, "funding_agency": {},
                "period_of_performance": {"start_date": "2024-01-01", "end_date": "2027-01-01"},
                "psc_hierarchy": {"base_code": {"code": "J063"}},
                "naics_hierarchy": {"base_code": {"code": "541512"}}}

    client.post.side_effect = fake_post
    client.get.side_effect = fake_get
    narrow = {"is_pure_play_vendor": False, "standalone_keywords": ["C-CURE 9000"],
              "context_gated_keywords": [], "probe_table": [],
              "positive_domain_context_regex": "\\bc-cure 9000\\b",
              "negative_drop_regex": "\\bham\\b"}
    cert = await run_audit_core(client, ["C-CURE 9000"], "2023-10-01", "2026-12-31",
                                manifest=narrow)
    assert cert["checks"]["AT-04"]["status"] == "FAIL"
    assert gid in cert["checks"]["AT-04"]["detail"]


@pytest.mark.asyncio
async def test_idv_post_endpoints_use_trailing_slash():
    """Regression: DRF-router idvs/* POSTs 301 slashless requests to the
    slashed path (bare 301, no followable redirect for POST bodies);
    idvs/awards and idvs/activity must end with '/'."""
    from usa_spending_mcp_server.tools.cscrm_idv import traverse_idv_core

    seen: list[str] = []
    client = AsyncMock()

    async def fake_get(endpoint, params=None):
        if endpoint.startswith("idvs/amounts"):
            return {"child_award_count": 0, "child_award_total_obligation": 0.0}
        return {"piid": "11316023A0004EOP", "base_and_all_options": 100.0,
                "total_obligation": 0.0, "type_description": "BPA",
                "period_of_performance": {}}

    async def fake_post(endpoint, payload):
        seen.append(endpoint)
        return {"results": [], "page_metadata": {"hasNext": False}}

    client.get.side_effect = fake_get
    client.post.side_effect = fake_post
    await traverse_idv_core(client, ["CONT_IDV_11316023A0004EOP_1100"])
    idv_posts = [e for e in seen if e.startswith("idvs/")]
    assert idv_posts, "expected idvs/* POST calls"
    assert all(e.endswith("/") for e in idv_posts), idv_posts
