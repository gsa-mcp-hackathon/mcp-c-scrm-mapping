"""Phase 3c tests: Stage 5 subawards, Stage 6 matrix, certification (mocked)."""

from decimal import Decimal
from typing import Any

import httpx
import pytest

from usaspending_cscrm_mcp.certification import (
    GateResult,
    issue_certificate,
    verify_certificate,
)
from usaspending_cscrm_mcp.client import USAspendingClient
from usaspending_cscrm_mcp.execution import RunBudgets
from usaspending_cscrm_mcp.models import (
    AwardRecord,
    FailedAwardLogEntry,
    QueryObservation,
    WindowAccounting,
)
from usaspending_cscrm_mcp.pipeline.stage4_adjudicate import Adjudication
from usaspending_cscrm_mcp.pipeline.stage5_subawards import run_stage5
from usaspending_cscrm_mcp.pipeline.stage6_matrix import build_matrix, check_completeness


def mock_client(post_map: dict[str, Any]):
    def handler(request: httpx.Request) -> httpx.Response:
        import json as _j
        if request.method == "POST" and request.url.path in post_map:
            value = post_map[request.url.path]
            if callable(value):
                return value(_j.loads(request.content.decode() or "{}"))
            return value
        return httpx.Response(404, json={})
    c = USAspendingClient()
    c.client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="https://api.usaspending.gov")
    return c


def sub_row(sid="S1", desc="Zorpcore Widget site licenses", amt=5000,
            prime="PIID-1", pgid="G1", name="SUB CO"):
    return {"Sub-Award ID": sid, "Sub-Award Description": desc, "Sub-Award Amount": amt,
            "Prime Award ID": prime, "prime_award_generated_internal_id": pgid,
            "Sub-Awardee Name": name}


# ---- Stage 5 -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stage5_ledger_and_adjudicated_sweep():
    def prime_pages(body):
        return httpx.Response(200, json={
            "results": [sub_row(), sub_row(sid="S2", amt=100)], "page_metadata": {}})

    def sweep_pages(body):
        page = int(body.get("page", 1))
        if page == 1:
            rows = [sub_row(sid="K1", desc="Zorpcore Widget deployment",
                            amt=9000, prime="PIID-9", pgid="G9"),
                    sub_row(sid="D1", desc="office furniture", amt=50,
                            prime="PIID-2", pgid="G2")]
            return httpx.Response(200, json={"results": rows,
                                             "page_metadata": {"hasNext": False}})
        return httpx.Response(200, json={"results": [], "page_metadata": {}})

    c = mock_client({"/api/v2/subawards/": prime_pages,
                     "/api/v2/search/spending_by_award/": sweep_pages})
    out = await run_stage5(c, ["G1"], sweep_terms=["Zorpcore Widget"],
                           standalone_terms=["Zorpcore Widget"], gated_terms=[])
    assert out.status == "COMPLETE"
    assert out.ledgers[0].subaward_count == 2
    assert out.sweep_hits_total == 2
    assert len(out.kept_hits) == 1
    assert out.kept_hits[0].sub_award_id == "K1"
    assert out.kept_hits[0].tier == "Tier-1A"
    assert out.sweep_warning is None
    await c.aclose()


@pytest.mark.asyncio
async def test_stage5_fail_soft_and_sweep_warning():
    def boom(body):
        raise RuntimeError("sweep down")
    c = mock_client({"/api/v2/subawards/": httpx.Response(500, json={}),
                     "/api/v2/search/spending_by_award/": boom})
    out = await run_stage5(c, ["GZ"], sweep_terms=["Zorpcore Widget"],
                           standalone_terms=["Zorpcore Widget"])
    assert out.ledgers[0].error is not None
    assert out.sweep_warning is not None and "sweep down" in out.sweep_warning
    assert out.kept_hits == []
    await c.aclose()


@pytest.mark.asyncio
async def test_stage5_incomplete_on_budget():
    c = mock_client({"/api/v2/subawards/": httpx.Response(200, json={"results": []})})
    out = await run_stage5(c, ["G1", "G2"], budgets=RunBudgets(max_pages=0))
    assert out.status == "INCOMPLETE"
    await c.aclose()


# ---- Stage 6 -------------------------------------------------------------------

def hydrated(gid="G1", funder="Dept of X", awarder="Dept of X", net=100, pos=100,
             neg=0, life=1000, ghost=False, assisted=None):
    return AwardRecord(
        gid=gid, funding_agency=funder, awarding_agency=awarder,
        lifetime_obligated=Decimal(str(life)),
        window=WindowAccounting(window_positive_obligations=Decimal(str(pos)),
                                window_negative_deobligations=Decimal(str(neg)),
                                window_net_obligations=Decimal(str(net)),
                                window_transaction_count=1,
                                window_activity_status="POSITIVE_ONLY",
                                transaction_population_complete=True),
        ghost_historical=ghost,
        historical_reason="TERMINATED_MOD_C1_C4" if ghost else None,
        assisted_classification=assisted)


def adj(gid="G1", tier="Tier-1A", display="Tier-1A"):
    return Adjudication(gid=gid, tier=tier, display=display, rule="KEEP")


def test_matrix_cohorts_proof_and_assisted_credit():
    records = [hydrated("G1"), hydrated("G2", net=-20, pos=0, neg=-20),
               hydrated("G3", ghost=True),
               hydrated("G4", funder="Dept of Y", awarder="Dept of X",
                        assisted="GENUINE_ASSISTED_ACQUISITION")]
    adjs = {"G1": adj("G1"), "G2": adj("G2"),
            "G3": adj("G3", display="HISTORICAL"), "G4": adj("G4")}
    res = build_matrix(records, adjs)
    by_key = {(r.agency, r.display): r for r in res.matrix}
    assert by_key[("Dept of X", "Tier-1A")].award_count == 2
    assert by_key[("Dept of X", "Tier-1A")].window_net == Decimal("80")
    assert by_key[("Dept of X", "HISTORICAL")].award_count == 1
    # assisted credit: funder Y, servicing ledger X->Y
    assert ("Dept of Y", "Tier-1A") in by_key
    assert res.servicing_ledger[0].servicing_agency == "Dept of X"
    assert res.servicing_ledger[0].funding_agency == "Dept of Y"
    # phantom rows impossible: every row has proof
    assert all(r.proof_gids for r in res.matrix)
    assert res.totals["Tier-1A"] == "180"
    # dropped awards never appear
    res2 = build_matrix(records, {"G1": adj("G1"), "G2": Adjudication(
        gid="G2", tier="DROPPED", rule="X"),
        "G3": adj("G3", display="HISTORICAL"), "G4": adj("G4")})
    assert ("Dept of X", "Tier-1A") in {(r.agency, r.display) for r in res2.matrix}
    assert res2.totals["Tier-1A"] == "200"
    assert res.matrix_hash and len(res.matrix_hash) == 64


def test_completeness_parity_violations():
    obs = [QueryObservation(query_id="q1", endpoint="e", canonical_filter_hash="h",
                            reported_count=10, fetched_count=10, page_size=10,
                            terminal_has_next=False, complete=True),
           QueryObservation(query_id="q2", endpoint="e", canonical_filter_hash="h",
                            reported_count=10, fetched_count=7, page_size=10,
                            terminal_has_next=True, complete=False)]
    rep = check_completeness(obs, 10, [FailedAwardLogEntry(gid="GX", operation="op",
                                                          endpoint="e",
                                                          error_category="E")],
                             ["limitation A"])
    assert rep.populations_complete == 1
    assert len(rep.parity_violations) == 1 and "q2" in rep.parity_violations[0]
    assert rep.unresolved_limitations == ["limitation A"]
    assert rep.awards_failed == 1


# ---- certification ---------------------------------------------------------------

def test_certificate_unsigned_and_tamper_evident():
    cert = issue_certificate(target_name="Zorpcore", window_start="2023-10-01",
                             window_end="2026-09-30", manifest_hash="mh",
                             matrix_hash="mx",
                             gate_results=[GateResult(gate_id="AT-00", status="PASS")],
                             totals={"Tier-1A": "180"})
    assert cert.signature.status == "UNSIGNED"
    verdict = verify_certificate(cert.model_dump(mode="json"))
    assert verdict["verdict"] == "INVALID"
    assert any("UNSIGNED" in r for r in verdict["reasons"])
    mutated = cert.model_dump(mode="json")
    mutated["totals"] = {"Tier-1A": "999"}
    assert verify_certificate(mutated)["verdict"] == "INVALID"
    gates_fail = cert.model_dump(mode="json")
    gates_fail["gate_results"] = [{"gate_id": "AT-00", "status": "FAIL", "detail": "x"}]
    assert any("AT-00" in r
               for r in verify_certificate(gates_fail)["reasons"])
