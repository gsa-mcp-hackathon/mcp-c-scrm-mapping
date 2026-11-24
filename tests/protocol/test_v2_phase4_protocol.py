"""Phase 4 protocol tests: 10 tools via in-memory MCP (mocked transport)."""


import httpx
import pytest
from fastmcp import Client

from usaspending_cscrm_mcp.client import USAspendingClient
from usaspending_cscrm_mcp.server import create_server
from usaspending_cscrm_mcp.tools import ServerDeps, configure

EXPECTED_TOOLS = ["target_triage", "agency_exposure_search", "award_narrative_fetch",
                  "award_search", "vehicle_hierarchy", "recipient_graph",
                  "award_context", "lexical_review", "subaward_sweep", "audit_run"]

TERM = "Zorpcore Widget"


def sample_row(desc="Zorpcore Widget enterprise subscription",
               uei="U-ZORP", gid="GS1"):
    return {"Description": desc, "Recipient UEI": uei, "NAICS Code": "541512",
            "PSC Code": "D399", "NAICS Description": "IT services",
            "PSC Description": "IT equipment", "Award ID": "P-" + gid,
            "Recipient Name": "ZORP INC", "Awarding Agency": "Dept of X",
            "Funding Agency": "Dept of X", "Award Amount": 100,
            "generated_internal_id": gid}


def detail(gid="G1", desc="Zorpcore Widget enterprise licenses"):
    return {"piid": "P-" + gid, "description": desc, "total_obligation": 1000,
            "base_and_all_options": 5000, "parent_award": {},
            "recipient": {"recipient_name": "ZORP INC", "recipient_uei": "U-ZORP"},
            "awarding_agency": {"toptier_agency": {"code": "001", "name": "AA"},
                                "subtier_agency": {"name": "AAS"}},
            "funding_agency": {"toptier_agency": {"code": "001", "name": "FA"},
                               "subtier_agency": {"name": "FAS"}},
            "naics_hierarchy": {"base_code": {"code": "541512"}},
            "psc_hierarchy": {"base_code": {"code": "D399"}},
            "period_of_performance": {"start_date": "2023-10-01", "end_date": "2026-09-30"}}


def txrow(gid, amt, mod="0"):
    return {"Award ID": "P-" + gid, "generated_internal_id": gid, "Action Date": "2024-05-01",
            "Transaction Amount": amt, "Transaction Description": "Zorpcore Widget license",
            "Award Type": "A", "Action Type": "A", "Recipient Name": "ZORP INC",
            "Recipient UEI": "U-ZORP", "Awarding Agency": "AA", "Awarding Sub Agency": "AAS",
            "Funding Agency": "FA", "Funding Sub Agency": "FAS", "Mod": mod,
            "internal_id": "i" + mod}


def backend(request: httpx.Request) -> httpx.Response:
    import json as _j
    path = request.url.path
    if request.method == "GET":
        if path.startswith("/api/v2/awards/") and not path.startswith("/api/v2/awards/funding"):
            gid = path.split("/api/v2/awards/")[1].strip("/")
            if gid in ("G1", "G2", "G3"):
                return httpx.Response(200, json=detail(gid))
            if gid == "V1":
                d = detail("V1", "Zorpcore Widget bulk vehicle")
                return httpx.Response(200, json=d)
            return httpx.Response(404, json={})
        if path.startswith("/api/v2/idvs/amounts/"):
            return httpx.Response(200, json={"child_award_count": 1,
                                             "child_award_total_obligation": 400})
        if path.startswith("/api/v2/recipient/children/"):
            uei = path.split("/api/v2/recipient/children/")[1].strip("/")
            if uei == "U-ZORP":
                return httpx.Response(200, json=[{"recipient_name": "ZORP CHILD"}])
            return httpx.Response(400, json={
                "detail": f"Recipient is not listed as a parent: '{uei}'."})
        return httpx.Response(404, json={})
    body = _j.loads(request.content.decode() or "{}")
    if path == "/api/v2/search/spending_by_award_count/":
        kw = (body.get("filters", {}).get("keywords") or [""])[0]
        n = 2 if kw == TERM else 0
        return httpx.Response(200, json={"results": {"contracts": n, "idvs": 0}})
    if path == "/api/v2/search/spending_by_award/":
        if body.get("subawards"):
            kw = (body.get("filters", {}).get("keywords") or [""])[0]
            if kw == TERM:
                return httpx.Response(200, json={"results": [{
                    "Sub-Award ID": "K1", "Sub-Award Description": "Zorpcore Widget rollout",
                    "Sub-Award Amount": 9000, "Prime Award ID": "P-G1",
                    "prime_award_generated_internal_id": "G1"}],
                    "page_metadata": {"hasNext": False}})
            return httpx.Response(200, json={"results": [], "page_metadata": {}})
        filters = body.get("filters", {})
        kw = (filters.get("keywords") or [""])[0]
        if "award_type_codes" not in filters:  # Stage-0 probe
            rows = [sample_row(gid=f"GS{i}") for i in range(25)] if kw == TERM else []
            return httpx.Response(200, json={"results": rows,
                                             "page_metadata": {"total": len(rows),
                                                               "hasNext": False}})
        codes = filters.get("award_type_codes", [])
        group = "contracts" if codes and codes[0] == "A" else "idvs"
        if kw == TERM and group == "contracts" and int(body.get("page", 1)) == 1:
            return httpx.Response(200, json={"results": [sample_row(gid="G1"),
                                                         sample_row(gid="G2")],
                                             "page_metadata": {"total": 2,
                                                               "hasNext": False}})
        return httpx.Response(200, json={"results": [], "page_metadata": {}})
    if path == "/api/v2/search/spending_by_transaction/":
        return httpx.Response(200, json={"results": [txrow("G1", 100), txrow("G2", 200)],
                                         "page_metadata": {"hasNext": False}})
    if path == "/api/v2/subawards/":
        return httpx.Response(200, json={"results": [], "page_metadata": {}})
    if path == "/api/v2/idvs/awards/":
        return httpx.Response(200, json={"results": [
            {"description": "Zorpcore Widget task order",
             "generated_unique_award_id": "G3"}], "page_metadata": {}})
    if path == "/api/v2/idvs/activity/":
        return httpx.Response(200, json={"results": [{"recipient_id": "R1"}],
                                         "page_metadata": {}})
    return httpx.Response(404, json={})


def make_mock_client() -> USAspendingClient:
    c = USAspendingClient()
    c.client = httpx.AsyncClient(transport=httpx.MockTransport(backend),
                                 base_url="https://api.usaspending.gov")
    return c


@pytest.fixture
def mcp_client():
    configure(ServerDeps(client_factory=make_mock_client))
    return Client(create_server(ServerDeps(client_factory=make_mock_client)))


def manifest():
    return {"target_name": "Zorpcore", "input_hash": "h",
            "window_start": "2023-10-01", "window_end": "2026-09-30",
            "standalone_terms": [TERM], "context_gated_terms": [],
            "rejected_terms": [], "term_scores": [], "verified_oem_ueis": ["U-ZORP"],
            "status": "READY"}


@pytest.mark.asyncio
async def test_tools_list_exactly_ten(mcp_client):
    async with mcp_client as client:
        tools = await client.list_tools()
    assert sorted(t.name for t in tools) == sorted(EXPECTED_TOOLS)


@pytest.mark.asyncio
async def test_target_triage_call(mcp_client):
    async with mcp_client as client:
        out = await client.call_tool("target_triage", {
            "target_query": "Zorpcore", "candidate_terms": [TERM],
            "verified_oem_ueis": ["U-ZORP"],
            "from_date": "2023-10-01", "to_date": "2026-09-30"})
    assert out.data["status"] == "READY"
    assert out.data["manifest"]["standalone_terms"] == [TERM]


@pytest.mark.asyncio
async def test_award_search_and_exposure(mcp_client):
    async with mcp_client as client:
        res = await client.call_tool("award_search", {"target_manifest": manifest()})
        assert sorted(h["gid"] for h in res.data["hits"]) == ["G1", "G2"]
        exp = await client.call_tool("agency_exposure_search",
                                     {"target_manifest": manifest()})
        assert exp.data["agencies"]["Dept of X"]["hits"] == 2  # set-deduped per hit


@pytest.mark.asyncio
async def test_context_narrative_recipient_vehicle(mcp_client):
    async with mcp_client as client:
        ctx = await client.call_tool("award_context", {"award_gids": ["G1"]})
        assert ctx.data["status"] == "COMPLETE"
        assert ctx.data["hydrated"][0]["window"]["window_net_obligations"] == "100"
        nar = await client.call_tool("award_narrative_fetch", {"award_gids": ["G1"]})
        assert "Zorpcore Widget" in nar.data["narratives"][0]["base_description"]
        rg = await client.call_tool("recipient_graph",
                                    {"ueis": ["U-ZORP"],
                                     "verified_oem_ueis": ["U-ZORP"]})
        assert rg.data["nodes"][0]["channel"] == "OEM_DIRECT_SUBSIDIARY"
        rg2 = await client.call_tool("recipient_graph", {"ueis": ["U-STRANGER"]})
        assert rg2.data["nodes"][0]["channel"] == "THIRD_PARTY_INTEGRATOR"
        veh = await client.call_tool("vehicle_hierarchy", {
            "parent_gids": ["V1"], "standalone_terms": [TERM]})
        assert veh.data["vehicles"][0]["classification"] == "SINGLETON_OBSERVED"


@pytest.mark.asyncio
async def test_lexical_review_and_subaward_sweep(mcp_client):
    rec = {"gid": "G1", "base_description": "Zorpcore Widget enterprise subscription",
           "recipient_uei": "U-OTHER", "naics_code": "541512", "psc_code": "D399"}
    async with mcp_client as client:
        lex = await client.call_tool("lexical_review", {
            "records": [rec], "standalone_terms": [TERM]})
        assert lex.data["adjudications"][0]["tier"] == "Tier-1A"
        assert lex.data["adjudications"][0]["evidence"]["substring_verified"] is True
        sweep = await client.call_tool("subaward_sweep", {
            "prime_gids": ["G1"], "sweep_terms": [TERM],
            "standalone_terms": [TERM]})
        assert sweep.data["kept_hits"][0]["sub_award_id"] == "K1"


@pytest.mark.asyncio
async def test_audit_run_end_to_end_prime_totals_unaffected(mcp_client):
    async with mcp_client as client:
        out = await client.call_tool("audit_run", {"target_manifest": manifest()})
    assert out.data["status"] == "COMPLETE"
    totals = out.data["matrix"]["totals"]
    assert totals["Tier-1A"] == "300"  # 100 + 200 prime only; no subaward dollars
    cert = out.data["certificate"]
    assert cert["signature"]["status"] == "UNSIGNED"
    assert cert["manifest_hash"] and cert["matrix_hash"]
    assert out.data["subawards"]["kept_hits"][0]["prime_gid"] == "G1"


@pytest.mark.asyncio
async def test_recipient_non_parent_fails_soft(mcp_client):
    async with mcp_client as client:
        rg = await client.call_tool("recipient_graph", {"ueis": ["U-LEAF"]})
    node = rg.data["nodes"][0]
    assert node["channel"] == "THIRD_PARTY_INTEGRATOR"
    assert node["error"] is not None


@pytest.mark.asyncio
async def test_oversized_invalid_unknown_inputs(mcp_client):
    async with mcp_client as client:
        with pytest.raises(Exception, match="exceeds limit"):
            await client.call_tool("award_context",
                                   {"award_gids": ["G"] * 501,
                                    "window_start": "2023-10-01",
                                    "window_end": "2026-09-30"})
        with pytest.raises(Exception, match="invalid target_manifest"):
            await client.call_tool("award_search",
                                   {"target_manifest": {"bogus": True}})
        try:
            await client.call_tool("no_such_tool", {})
            raise AssertionError("unknown tool should fail")
        except Exception as exc:  # noqa: BLE001, B017 - asserting on message below
            assert "no_such_tool" in str(exc)
