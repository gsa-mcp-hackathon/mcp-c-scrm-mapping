"""Phase 3b tests: Stages 2–4 with synthetic target terms (mocked transport)."""

from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from usaspending_cscrm_mcp.client import USAspendingClient
from usaspending_cscrm_mcp.execution import RunBudgets
from usaspending_cscrm_mcp.models import AwardRecord, FailedAwardLogEntry
from usaspending_cscrm_mcp.pipeline.stage2_hydrate import (
    classify_ghost,
    hydrate_awards,
)
from usaspending_cscrm_mcp.pipeline.stage3_idv import (
    classify_container,
    traverse_vehicle,
)
from usaspending_cscrm_mcp.pipeline.stage4_adjudicate import (
    adjudicate,
    apply_display,
    mask_boilerplate,
    term_re,
    to_evidence,
)

TERM = "Zorpcore Widget"


def detail(piid="P1", desc="Zorpcore Widget enterprise licenses",
           lifetime=1000, end="2026-09-30", a_top="001", f_top="001",
           naics="541512", psc="D399", uei="U-ZORP"):
    return {
        "piid": piid, "description": desc, "total_obligation": lifetime,
        "base_and_all_options": 5000,
        "parent_award": {}, "recipient": {"recipient_name": "ZORP INC", "recipient_uei": uei},
        "awarding_agency": {"toptier_agency": {"code": a_top, "name": "AA"},
                            "subtier_agency": {"name": "AAS"}},
        "funding_agency": {"toptier_agency": {"code": f_top, "name": "FA"},
                           "subtier_agency": {"name": "FAS"}},
        "naics_hierarchy": {"base_code": {"code": naics, "description": "IT"}},
        "psc_hierarchy": {"base_code": {"code": psc}},
        "period_of_performance": {"start_date": "2023-10-01", "end_date": end},
    }


def txrow(gid="G1", mod="0", date_="2024-05-01", amt=100, desc="Zorpcore Widget license",
          action="A", agency="AA"):
    return {"Award ID": "P1", "generated_internal_id": gid, "Action Date": date_,
            "Transaction Amount": amt, "Transaction Description": desc,
            "Award Type": "A", "Action Type": action, "Recipient Name": "ZORP INC",
            "Recipient UEI": "U-ZORP", "Awarding Agency": agency,
            "Awarding Sub Agency": "AAS", "Funding Agency": "FA",
            "Funding Sub Agency": "FAS", "Mod": mod, "internal_id": "iid-" + mod}


def mock_client(routes: dict[tuple[str, str], Any]):
    def handler(request: httpx.Request) -> httpx.Response:
        import json as _j
        for (m, suffix), value in routes.items():
            if request.method == m and request.url.path.endswith(suffix):
                if callable(value):
                    return value(_j.loads(request.content.decode() or "{}"))
                return value
        return httpx.Response(404, json={"detail": "no route"})
    c = USAspendingClient()
    c.client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="https://api.usaspending.gov")
    return c


def tx_pages(*pages):
    def _h(body):
        page = int(body.get("page", 1))
        rows, total, has_next = pages[page - 1]
        return httpx.Response(200, json={"results": rows,
                                         "page_metadata": {"total": total,
                                                           "hasNext": has_next}})
    return _h


# ---- Stage 2 -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hydrate_sums_unfiltered_and_decimal():
    rows = [txrow(amt=100, date_="2024-05-01"), txrow(mod="1", amt=-30, date_="2024-06-01",
                                                      desc="ADMIN DEOBLIGATION"),
            txrow(mod="2", amt=0, date_="2024-07-01"),
            txrow(mod="3", amt=999, date_="2022-01-01"),  # out of window
            {**txrow(mod="4", amt=50), "generated_internal_id": "G-OTHER"}]
    c = mock_client({
        ("GET", "awards/G1/"): httpx.Response(200, json=detail()),
        ("POST", "search/spending_by_transaction/"): tx_pages((rows, 5, False)),
        ("POST", "awards/funding/"): httpx.Response(200, json={"results": []}),
    })
    out = await hydrate_awards(c, ["G1"], "2023-10-01", "2026-09-30")
    assert out["status"] == "COMPLETE"
    rec = out["hydrated"][0]
    assert isinstance(rec, AwardRecord)
    assert rec.window.window_positive_obligations == Decimal("100")
    assert rec.window.window_negative_deobligations == Decimal("-30")
    assert rec.window.window_net_obligations == Decimal("70")
    assert rec.window.window_transaction_count == 3
    assert rec.window.window_activity_status == "MIXED_NONZERO_NET"
    assert rec.hydration_complete is True and not rec.ghost_historical
    await c.aclose()


@pytest.mark.asyncio
async def test_hydrate_ghost_terminated_and_ended():
    rows = [txrow(gid="G9", date_="2024-05-01", action="C1")]
    c = mock_client({
        ("GET", "awards/G9/"): httpx.Response(200, json=detail()),
        ("POST", "search/spending_by_transaction/"): tx_pages((rows, 1, False)),
    })
    out = await hydrate_awards(c, ["G9"], "2023-10-01", "2026-09-30")
    rec = out["hydrated"][0]
    assert rec.ghost_historical and rec.historical_reason == "TERMINATED_MOD_C1_C4"
    assert rec.award_lifecycle_status == "TERMINATED_MOD"
    await c.aclose()
    assert classify_ghost([], Decimal("0"), Decimal("0"), "2020-01-01",
                           "2026-09-30") == (True, "ZERO_OR_NEGATIVE_LIFETIME")


@pytest.mark.asyncio
async def test_hydrate_fail_soft_and_incomplete():
    c = mock_client({
        ("GET", "awards/GOOD/"): httpx.Response(200, json=detail()),
        ("GET", "awards/BAD/"): httpx.Response(404, json={"detail": "x"}),
        ("POST", "search/spending_by_transaction/"): tx_pages(
            ([txrow(gid="GOOD")], 1, True), ([txrow(gid="GOOD", mod="1")], 1, False)),
    })
    out = await hydrate_awards(c, ["GOOD", "BAD"], "2023-10-01", "2026-09-30",
                               budgets=RunBudgets(max_pages=1),
                               signing_key=b"k", caller_id="u1")
    assert out["status"] == "INCOMPLETE" and out["continuation"] is not None
    assert isinstance(out["failed"], list)
    out2 = await hydrate_awards(c, ["BAD"], "2023-10-01", "2026-09-30")
    assert out2["status"] == "COMPLETE"
    assert isinstance(out2["failed"][0], FailedAwardLogEntry)
    assert out2["failed"][0].transient is False
    await c.aclose()


@pytest.mark.asyncio
async def test_hydrate_assisted_acquisition():
    c = mock_client({
        ("GET", "awards/GA/"): httpx.Response(200, json=detail(a_top="097", f_top="096")),
        ("POST", "search/spending_by_transaction/"): tx_pages(([txrow(gid="GA")], 1, False)),
        ("POST", "awards/funding/"): httpx.Response(200, json={"results": [{"x": 1}]}),
    })
    out = await hydrate_awards(c, ["GA"], "2023-10-01", "2026-09-30")
    assert out["hydrated"][0].assisted_classification == "INTERNAL_USACE_CIVIL_WORKS_MAPPING"
    await c.aclose()


# ---- Stage 3 -------------------------------------------------------------------

def test_classify_container_gates():
    assert classify_container(10, True, True, 0.5, 1)[0] == "DEDICATED_SINGLE_PURPOSE_IDV"
    assert classify_container(10, False, True, 0.25, 1)[1] == "gate-B-density"
    assert classify_container(60, True, True, 0.9, 1)[0] == "BROAD_MULTI_PURPOSE_IDV"
    assert classify_container(10, True, True, 0.9, 4)[1] == "broad-multi-awardee-matoc"
    assert classify_container(0, False, False, 0.0, 0)[1] == "broad-unverified-homogeneity"
    assert classify_container(10, True, True, 0.05, 1)[1] == "gate-A-description"


def keep_zorpcore(desc):
    return term_re(TERM).search(desc or "")


def vehicle_detail(children_expected=2):
    d = detail(piid="VIDV", desc="Zorpcore Widget bulk vehicle")
    d["base_and_all_options"] = 1000
    return d


@pytest.mark.asyncio
async def test_traverse_dedicated_and_broad():
    kids = [{"description": "Zorpcore Widget order 1", "generated_unique_award_id": "GC1"},
            {"description": "Zorpcore Widget order 2", "generated_unique_award_id": "GC2"}]
    activity = [{"recipient_id": "R1"}, {"recipient_id": "R1"}]
    amounts = {"child_award_count": 2, "child_award_total_obligation": 400}
    c = mock_client({
        ("GET", "awards/VD/"): httpx.Response(200, json=vehicle_detail()),
        ("GET", "idvs/amounts/VD/"): httpx.Response(200, json=amounts),
        ("POST", "idvs/awards/"): tx_pages((kids, 2, False)),
        ("POST", "idvs/activity/"): tx_pages((activity, 2, False)),
    })
    res = await traverse_vehicle(c, "VD", keep_zorpcore, today=date(2026, 1, 1))
    assert res is not None
    assert res.classification == "DEDICATED_SINGLE_PURPOSE_IDV"
    assert res.keyword_matched_children == 2
    assert res.parity_ok is True and res.child_count == res.expected_child_count == 2
    assert res.ceiling_burn == pytest.approx(0.4)
    assert len(res.edges) == 2 and res.edges[0].parent_gid == "VD"
    await c.aclose()

    big = [{"description": "", "generated_unique_award_id": f"GX{i}"}
           for i in range(5)]
    c2 = mock_client({
        ("GET", "awards/VB/"): httpx.Response(200, json=vehicle_detail()),
        ("GET", "idvs/amounts/VB/"): httpx.Response(
            200, json={"child_award_count": 5, "child_award_total_obligation": 0}),
        ("POST", "idvs/awards/"): tx_pages((big, 5, False)),
        ("POST", "idvs/activity/"): tx_pages(([], 0, False)),
    })
    res2 = await traverse_vehicle(c2, "VB", keep_zorpcore)
    assert res2 is not None
    assert res2.classification == "BROAD_MULTI_PURPOSE_IDV"
    assert res2.traversal == "enumerated-homogeneity-unverified"
    assert res2.tier2_inheritance_blocked is True
    assert res2.parity_ok is True
    assert res2.ceiling_burn is None
    await c2.aclose()


@pytest.mark.asyncio
async def test_traverse_dedicated_parity_unverified_without_amounts():
    """Regression: dedicated parity must be explicit — True on match,
    None (never silent False, never missing) when amounts are unavailable."""
    kids = [{"description": "Zorpcore Widget order 1", "generated_unique_award_id": "GC1"},
            {"description": "Zorpcore Widget order 2", "generated_unique_award_id": "GC2"}]
    activity = [{"recipient_id": "R1"}, {"recipient_id": "R1"}]
    c = mock_client({
        ("GET", "awards/VD2/"): httpx.Response(200, json=vehicle_detail()),
        ("GET", "idvs/amounts/VD2/"): httpx.Response(404, json={}),
        ("POST", "idvs/awards/"): tx_pages((kids, 2, False)),
        ("POST", "idvs/activity/"): tx_pages((activity, 2, False)),
    })
    res = await traverse_vehicle(c, "VD2", keep_zorpcore, today=date(2026, 1, 1))
    assert res is not None
    assert res.classification == "UNKNOWN_DEDICATION"
    assert res.parity_ok is None and res.global_child_count is None
    await c.aclose()


@pytest.mark.asyncio
async def test_traverse_parent_failure_isolates():
    c = mock_client({("GET", "awards/VX/"): httpx.Response(404, json={})})
    assert await traverse_vehicle(c, "VX", keep_zorpcore) is None
    await c.aclose()


# ---- Stage 4 -------------------------------------------------------------------

def rec(desc="Zorpcore Widget enterprise subscription", uei="U-OTHER",
        naics="541512", psc="D399", txs=()):
    return AwardRecord(gid="G1", base_description=desc, recipient_uei=uei,
                       naics_code=naics, psc_code=psc,
                       transaction_descriptions=list(txs))


KW = {"standalone_terms": [TERM], "gated_terms": ["license management"],
      "verified_ueis": set(), "homograph_ueis": set()}


def test_adjudicate_standalone_and_inflection_guard():
    a = adjudicate(rec(), **KW)
    assert (a.tier, a.rule, a.basis) == ("Tier-1A", "KEEP_MANIFEST_PRODUCT_MATCH",
                                         "product_associated")
    b = adjudicate(rec(desc="Zorpcore Widgeting process improvements"), **KW)
    assert (b.tier, b.rule) == ("DROPPED", "POLY_NO_CONTEXT")


def test_adjudicate_boilerplate_and_veto():
    assert "diligently" not in mask_boilerplate("prosecute the work diligently")
    v = adjudicate(rec(desc="access control training course", naics="236220",
                       txs=["janitorial services quarterly"]), **{
        "standalone_terms": [], "gated_terms": ["training"],
        "verified_ueis": set(), "homograph_ueis": set()})
    assert v.rule == "DOMAIN_TAXONOMY_VETO_GATED_ONLY"
    v2 = adjudicate(rec(desc="access control training course software subscription",
                        naics="236220"), **{
        "standalone_terms": [], "gated_terms": ["training"],
        "verified_ueis": set(), "homograph_ueis": set()})
    assert v2.tier == "Tier-2"


def test_adjudicate_homograph_conglomerate_competitor():
    h = adjudicate(rec(desc="quarterly license management review", uei="U-DOPPEL"),
                   **{**KW, "homograph_ueis": {"U-DOPPEL"}})
    assert h.rule == "ENTITY_HOMOGRAPH_NO_PRODUCT_TOKEN"
    # homograph WITH standalone token survives
    h2 = adjudicate(rec(desc="Zorpcore Widget license", uei="U-DOPPEL"),
                    **{**KW, "homograph_ueis": {"U-DOPPEL"},
                       "standalone_terms": [TERM]})
    assert h2.tier == "Tier-1A"
    cong = adjudicate(rec(desc="holding company quarterly report", uei="U-SUB"),
                      **{**KW, "standalone_terms": [], "gated_terms": [],
                         "verified_ueis": {"U-SUB"}, "is_conglomerate": True})
    assert cong.rule == "CONGLOMERATE_NAME_ONLY_NO_PRODUCT_TEXT"
    sib = adjudicate(rec(desc="Zorp analytics dashboard renewal", uei="U-SUB"),
                     **{**KW, "standalone_terms": [], "gated_terms": [],
                        "verified_ueis": {"U-SUB"}, "is_conglomerate": True,
                        "sibling_terms": ["Zorp Analytics"],
                        "target_name": "Zorp Widget"})
    assert (sib.tier, sib.rule) == ("Tier-1C", "OEM_SIBLING_PRODUCT_LINE")
    comp = adjudicate(rec(desc="Zorpcore Widget with Rivalsoft bundle"),
                      **{**KW, "competitor_rx": r"\brivalsoft\b"})
    assert comp.tier == "Tier-1B" and comp.competing == ["rivalsoft"]
    ctx = adjudicate(rec(desc="enterprise security operations center"),
                     **{**KW, "standalone_terms": [], "gated_terms": [],
                        "domain_context_rx": r"\bsecurity operations\b"})
    assert ctx.rule == "DOMAIN_CONTEXT_ONLY_NO_PRODUCT_TOKEN"


def test_display_and_evidence():
    a = adjudicate(rec(), **KW)
    ghost_rec = AwardRecord(gid="G1", base_description="Zorpcore Widget",
                            ghost_historical=True,
                            historical_reason="TERMINATED_MOD_C1_C4")
    ag = apply_display(adjudicate(ghost_rec, **KW), ghost_rec)
    assert ag.display == "HISTORICAL" and ag.tier == "Tier-1A"
    ev = to_evidence(a, rec())
    assert ev.kind == "lexical" and ev.substring_verified is True
    bad = to_evidence(a, rec(desc="unrelated text"))
    assert bad.substring_verified is False
