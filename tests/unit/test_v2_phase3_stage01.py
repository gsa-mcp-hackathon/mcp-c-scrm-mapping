"""Phase 3 tests: Stage 0 triage + Stage 1 search (mocked transport)."""

import httpx
import pytest

from usaspending_cscrm_mcp.client import USAspendingClient
from usaspending_cscrm_mcp.execution import RunBudgets
from usaspending_cscrm_mcp.pipeline.stage0_triage import (
    TargetManifest,
    detect_conglomerate_triggers,
    dispose_term,
    score_row,
    strip_trigger,
    triage_target,
)
from usaspending_cscrm_mcp.pipeline.stage1_search import gated_keep, run_stage1

UEIS = {"U-PURE-1"}


def row(desc="C-CURE 9000 access panels installed", uei="", naics="561621",
         psc="6350", naics_desc="Security Systems Services"):
    return {"Description": desc, "Recipient UEI": uei, "NAICS Code": naics,
            "PSC Code": psc, "NAICS Description": naics_desc,
            "PSC Description": "Physical access control equipment",
            "generated_internal_id": "G-" + (uei or "X")}


# ---- orthogonal scoring -------------------------------------------------------

def test_trigger_stripped_before_scoring():
    assert "c-cure" not in strip_trigger("INSTALL C-CURE PANELS", "C-CURE").lower()


def test_signal_a_pure_play_uei_survives_strip():
    r = row(desc="C-CURE paperwork filing", uei="U-PURE-1")
    assert score_row(r, "C-CURE", UEIS, True) == (True, "A")


def test_signal_a_denied_for_conglomerate():
    r = row(desc="C-CURE paperwork filing", uei="U-PURE-1")
    assert score_row(r, "C-CURE", UEIS, False)[0] is False


def test_signal_b_needs_taxonomy_plus_sibling():
    good = row(desc="C-CURE software license subscription")
    assert score_row(good, "C-CURE", set(), True) == (True, "B")
    lone = row(desc="C-CURE training course completion")
    assert score_row(lone, "training", set(), True)[0] is False
    construct = row(desc="C-CURE diligently performed", naics="236220",
                    naics_desc="Commercial Building Construction")
    assert score_row(construct, "C-CURE", set(), True)[0] is False


def test_ceiling_short_and_homograph_terms_never_standalone():
    assert dispose_term("ham", 1.0) != "STANDALONE"
    assert dispose_term("diligent", 1.0) != "STANDALONE"
    assert dispose_term("C-CURE 9000 panels", 0.95) == "STANDALONE"
    assert dispose_term("C-CURE 9000 panels", 0.6) == "CONTEXT_GATED"
    assert dispose_term("C-CURE 9000 panels", 0.1) == "REJECTED"


def test_conglomerate_triggers():
    assert detect_conglomerate_triggers(["JCI HVAC"], ["X"], 1)
    assert detect_conglomerate_triggers(["software"], ["Foo DBA Bar"], 1)
    assert detect_conglomerate_triggers(["software"], ["Plain Inc"], 9)
    assert detect_conglomerate_triggers(["software platform"], ["Plain Inc"], 1) == []


# ---- triage_target (mocked probe) -------------------------------------------------

def make_probe_client(rows_by_term, totals=None):
    def handler(request: httpx.Request) -> httpx.Response:
        import json as _j
        body = _j.loads(request.content.decode())
        term = (body.get("filters", {}).get("keywords") or [""])[0]
        codes = body.get("filters", {}).get("award_type_codes", [])
        tag = "C" if any(c in ("A", "B", "C", "D") for c in codes) else "I"
        rows = rows_by_term.get(term, [])
        # Groups are disjoint populations, and precision counts distinct
        # awards (a repeated row must not inflate true positives).
        out_rows = []
        for i, r in enumerate(rows):
            r2 = dict(r)
            r2["generated_internal_id"] = f"{r.get('generated_internal_id')}-{tag}-{i}"
            out_rows.append(r2)
        total = (totals or {}).get(term, len(rows))
        return httpx.Response(200, json={"results": out_rows,
                                         "page_metadata": {"total": total, "hasNext": False}})
    c = USAspendingClient()
    c.client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="https://api.usaspending.gov")
    return c


@pytest.mark.asyncio
async def test_triage_ready_manifest_with_scores():
    rows = [row(desc="C-CURE 9000 software subscription", uei="U-PURE-1")] * 20 + [row()] * 5
    c = make_probe_client({"C-CURE 9000": rows}, {"C-CURE 9000": 25})
    out = await triage_target(c, target_name="T", candidate_terms=["C-CURE 9000"],
                              verified_oem_ueis=["U-PURE-1"],
                              from_date="2023-10-01", to_date="2026-09-30")
    assert out["status"] == "READY"
    m = out["manifest"]
    assert m.standalone_terms == []
    assert m.context_gated_terms == ["C-CURE 9000"]
    assert m.term_scores[0].sample_size == 50
    assert out["manifest_hash"] == m.canonical_hash()
    assert TargetManifest.model_validate(m.model_dump()).canonical_hash() == out["manifest_hash"]
    await c.aclose()


@pytest.mark.asyncio
async def test_probe_splits_contract_idv_groups():
    """Regression: spending_by_award 422s mixed contract+IDV code sets —
    the probe must dispatch paired single-group calls, never one mixed call."""
    from usaspending_cscrm_mcp.execution import BudgetTracker
    from usaspending_cscrm_mcp.pipeline.stage0_triage import probe_term

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _j
        body = _j.loads(request.content.decode())
        codes = body.get("filters", {}).get("award_type_codes", [])
        mixed = (any(c in ("A", "B", "C", "D") for c in codes)
                 and any(str(c).startswith("IDV") for c in codes))
        if mixed:
            return httpx.Response(422, json={"detail": "mixed groups"})
        tag = "C" if any(c in ("A", "B", "C", "D") for c in codes) else "I"
        if request.url.path.endswith("spending_by_award_count/"):
            return httpx.Response(200, json={"results": {
                "contracts": 1 if tag == "C" else 0,
                "idvs": 1 if tag == "I" else 0}})
        r = row()
        r["generated_internal_id"] = f"G-X-{tag}"
        return httpx.Response(200, json={
            "results": [r], "page_metadata": {"total": 1, "hasNext": False}})

    c = USAspendingClient()
    c.client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="https://api.usaspending.gov")
    budgets = RunBudgets()
    out = await probe_term(c, "Arctic Wolf", set(), True,
                           "2023-10-01", "2026-12-31",
                           budgets, BudgetTracker(budgets))
    assert out.sample_size == 2 and out.total_count == 2
    await c.aclose()


@pytest.mark.asyncio
async def test_triage_needs_input_when_nothing_gates():
    c = make_probe_client({"zzz-no-match": []}, {"zzz-no-match": 0})
    out = await triage_target(c, target_name="T", candidate_terms=["zzz-no-match"],
                              verified_oem_ueis=[],
                              from_date="2023-10-01", to_date="2026-09-30")
    assert out["status"] == "NEEDS_USER_INPUT"
    assert out["manifest"].rejected_terms == ["zzz-no-match"]
    await c.aclose()


@pytest.mark.asyncio
async def test_triage_incomplete_yields_continuation():
    c = make_probe_client({"a": [row()], "b": [row()]})
    out = await triage_target(c, target_name="T", candidate_terms=["a", "b"],
                              verified_oem_ueis=[],
                              from_date="2023-10-01", to_date="2026-09-30",
                              budgets=RunBudgets(max_pages=1),
                              signing_key=b"k", caller_id="u1")
    assert out["status"] == "INCOMPLETE"
    assert out["continuation"] is not None
    await c.aclose()


# ---- Stage 1 (mocked search) ----------------------------------------------------------

def make_search_client(pages, seen_keywords=None):
    def handler(request: httpx.Request) -> httpx.Response:
        import json as _j
        body = _j.loads(request.content.decode())
        kw = (body.get("filters", {}).get("keywords") or [""])[0]
        (seen_keywords or []).append(kw)
        codes = body.get("filters", {}).get("award_type_codes", [])
        group = "contracts" if codes and codes[0] == "A" else "idvs"
        if request.url.path.endswith("spending_by_award_count/"):
            totals = {t for (k, g, _p), (_r, t, _h) in pages.items()
                      if k == kw and g == group}
            total = max(totals) if totals else 0
            return httpx.Response(200, json={"results": {
                "contracts": total if group == "contracts" else 0,
                "idvs": total if group == "idvs" else 0}})
        page = int(body.get("page", 1))
        key = (kw, group, page)
        rows, total, has_next = pages.get(key, ([], 0, False))
        return httpx.Response(200, json={"results": rows,
                                         "page_metadata": {"hasNext": has_next}})
    c = USAspendingClient()
    c.client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="https://api.usaspending.gov")
    return c


def srow(gid, desc="C-CURE 9000 software subscription", uei="U-PURE-1"):
    return {"generated_internal_id": gid, "Award ID": "PIID-" + gid,
            "Recipient Name": "PURE INC", "Recipient UEI": uei,
            "Awarding Agency": "Dept X", "Funding Agency": "Dept X",
            "Award Amount": 1000, "Description": desc,
            "NAICS Code": "541512", "PSC Code": "D399"}


def manifest_for():
    return TargetManifest(
        target_name="T", input_hash="h",
        window_start="2023-10-01", window_end="2026-09-30",
        standalone_terms=["C-CURE 9000"], context_gated_terms=["access control"],
        rejected_terms=["zzz-no-match"])


def test_gated_keep_rule():
    assert gated_keep(srow("G1"), "access control") is True
    assert gated_keep(srow("G2", desc="access control training course"), "access control") is False


@pytest.mark.asyncio
async def test_stage1_dual_vector_parity_and_dedupe():
    pages = {
        ("C-CURE 9000", "contracts", 1): ([srow("G1"), srow("G2")], 3, True),
        ("C-CURE 9000", "contracts", 2): ([srow("G3")], 3, False),
        ("C-CURE 9000", "idvs", 1): ([], 0, False),
        ("access control", "contracts", 1): (
            [srow("G1", desc="access control software platform deployment")], 1, False),
        ("access control", "idvs", 1): ([], 0, False),
    }
    seen_kw: list[str] = []
    c = make_search_client(pages, seen_kw)
    res = await run_stage1(c, manifest_for())
    assert res.status == "COMPLETE"
    assert sorted(h.gid for h in res.hits) == ["G1", "G2", "G3"]
    assert "zzz-no-match" not in seen_kw
    contracts_obs = [o for o in res.observations if o.query_id.endswith(":contracts")]
    assert any(o.reported_count == 3 and o.complete for o in contracts_obs)
    idvs_obs = [o for o in res.observations if o.query_id.endswith(":idvs")]
    assert all(o.complete and o.reported_count == o.fetched_count == 0
               for o in idvs_obs)
    await c.aclose()


@pytest.mark.asyncio
async def test_stage1_incomplete_continuation_roundtrip():
    pages = {("C-CURE 9000", "contracts", p): ([srow(f"G{p}")], 9, True)
             for p in range(1, 10)}
    c = make_search_client(pages)
    key = b"stage1-key"
    res = await run_stage1(c, manifest_for(),
                           budgets=RunBudgets(max_pages=3),
                           signing_key=key, caller_id="u1")
    assert res.status == "INCOMPLETE"
    assert res.continuation is not None
    from usaspending_cscrm_mcp.execution import ContinuationToken
    ct = ContinuationToken.decode(res.continuation, key)
    assert ct.cursor["vi"] == 0 and ct.cursor["page"] == 3
    await c.aclose()
