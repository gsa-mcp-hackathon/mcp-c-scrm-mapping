"""Phase 3b tests: subaward hydration, segregated matrix signal, banner render.

Covers the endpoint-specific adapters (exact live key spellings), the
no-blank invariant, the agency fallback chain, prime/subaward no-double-count,
and the AT-10 banner parse + render. No vendor or product names appear here;
fixtures use synthetic values with the real key spellings.
"""

import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from usaspending_cscrm_mcp.certification import issue_certificate
from usaspending_cscrm_mcp.client import USAspendingClient
from usaspending_cscrm_mcp.models import AwardRecord, WindowAccounting
from usaspending_cscrm_mcp.pipeline.stage4_adjudicate import Adjudication
from usaspending_cscrm_mcp.pipeline.stage5_subawards import (
    adapt_per_prime_row,
    adapt_sweep_row,
    run_stage5,
)
from usaspending_cscrm_mcp.pipeline.stage6_matrix import (
    SUBAWARD_DISPLAY,
    build_matrix,
    build_subaward_cohort_rows,
    resolve_subaward_agency,
)


def _renderer():
    path = Path(__file__).resolve().parents[2] / "evals" / "run_target_audit.py"
    spec = importlib.util.spec_from_file_location("run_target_audit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def per_prime_row(**kw: Any) -> dict[str, Any]:
    row = {"id": 9903747, "subaward_number": "CW99968",
           "description": "Example license reseller line item",
           "action_date": "2025-07-16", "amount": 266192.0,
           "recipient_name": "Example Reseller Corp"}
    row.update(kw)
    return row


def sweep_row(**kw: Any) -> dict[str, Any]:
    row = {"internal_id": "609261", "prime_award_internal_id": 292027610,
           "Sub-Award ID": "609261",
           "Sub-Awardee Name": "Example Subcontractor LLC",
           "Sub-Award Description": "Example subcontract quarterly services",
           "Sub-Award Amount": 137297.98, "Sub-Award Date": "2020-07-23",
           "Sub-Award Type": "sub-contract", "Prime Award ID": "0000000000000016",
           "prime_award_generated_internal_id": "CONT_AWD_EXAMPLE_GID",
           "Prime Recipient Name": "Example Prime LLC",
           "Awarding Agency": "Department of Energy", "Funding Agency": None}
    row.update(kw)
    return row


def _real_mock(post_map: dict[str, Any]) -> USAspendingClient:
    def handler(request: httpx.Request) -> httpx.Response:
        value = post_map.get(request.url.path)
        if value is None:
            return httpx.Response(404, json={})
        if callable(value):
            return value(json.loads(request.content.decode() or "{}"))
        return value

    client = USAspendingClient()
    client.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.usaspending.gov")
    return client


# ---- Stage 5 adapters --------------------------------------------------------


def test_per_prime_adapter_reads_snake_case_keys():
    rec = adapt_per_prime_row("G1", per_prime_row())
    assert rec.sub_award_id == "CW99968"
    assert rec.subawardee_name == "Example Reseller Corp"
    assert rec.amount == Decimal("266192.0")
    assert rec.description == "Example license reseller line item"
    assert rec.prime_gid == "G1"
    assert rec.prime_award_id == ""  # endpoint returns none; gid carries linkage
    assert rec.subaward_number == "CW99968"  # gate duck-type alias
    assert rec.completeness_defect is True  # agencies absent by endpoint design


def test_per_prime_adapter_missing_name_flags_defect():
    rec = adapt_per_prime_row("G1", per_prime_row(recipient_name="", amount=None))
    assert rec.subawardee_name == ""
    assert rec.amount is None
    assert rec.completeness_defect is True


def test_sweep_adapter_reads_title_case_keys():
    hit = adapt_sweep_row(sweep_row())
    assert hit.sub_award_id == "609261"
    assert hit.subawardee_name == "Example Subcontractor LLC"
    assert hit.amount == Decimal("137297.98")
    assert hit.prime_award_id == "0000000000000016"
    assert hit.prime_gid == "CONT_AWD_EXAMPLE_GID"
    assert hit.prime_recipient_name == "Example Prime LLC"
    assert hit.awarding_agency == "Department of Energy"
    assert hit.funding_agency == ""
    assert hit.subaward_number == "609261"  # gate duck-type alias
    assert hit.completeness_defect is False  # awarding agency present in source


def test_sweep_adapter_full_agencies_no_defect():
    hit = adapt_sweep_row(sweep_row(**{"Funding Agency": "Department of Defense"}))
    assert hit.funding_agency == "Department of Defense"
    assert hit.completeness_defect is False


def test_sweep_adapter_missing_name_flags_defect():
    hit = adapt_sweep_row(sweep_row(**{"Sub-Awardee Name": None}))
    assert hit.subawardee_name == ""
    assert hit.completeness_defect is True


async def test_run_stage5_no_silent_blanks_and_requests_name_fields():
    seen: dict[str, Any] = {}

    def sweep_pages(body: dict[str, Any]) -> httpx.Response:
        seen["fields"] = body.get("fields", [])
        return httpx.Response(200, json={"results": [sweep_row()],
                                         "page_metadata": {"hasNext": False}})

    def prime_pages(body: dict[str, Any]) -> httpx.Response:
        _ = body
        return httpx.Response(200, json={"results": [per_prime_row()],
                                         "page_metadata": {}})

    client = _real_mock({"/api/v2/subawards/": prime_pages,
                         "/api/v2/search/spending_by_award/": sweep_pages})
    out = await run_stage5(client, ["G1"], sweep_terms=["Example subcontract"],
                           standalone_terms=["Example subcontract"], gated_terms=[])
    await client.aclose()
    for key in ("Sub-Awardee Name", "Prime Recipient Name",
                "Awarding Agency", "Funding Agency"):
        assert key in seen["fields"]
    assert len(out.kept_hits) == 1
    hit = out.kept_hits[0]
    assert hit.subawardee_name == "Example Subcontractor LLC"
    assert hit.awarding_agency == "Department of Energy"
    ledger_rec = out.ledgers[0].subawards[0]
    assert ledger_rec.subawardee_name == "Example Reseller Corp"
    for obj in (hit, ledger_rec):
        blank_ok = bool(obj.subawardee_name) or obj.completeness_defect
        assert blank_ok, "silent blank: no name and no defect flag"


async def test_run_stage5_threads_sibling_params():
    def sweep_pages(body: dict[str, Any]) -> httpx.Response:
        _ = body
        return httpx.Response(200, json={"results": [sweep_row()],
                                         "page_metadata": {"hasNext": False}})

    client = _real_mock({"/api/v2/subawards/": httpx.Response(200, json={"results": []}),
                         "/api/v2/search/spending_by_award/": sweep_pages})
    out = await run_stage5(client, ["G1"], sweep_terms=["Example subcontract"],
                           standalone_terms=["Example subcontract"], gated_terms=[],
                           sibling_terms=["Example sibling"], target_name="Example target")
    await client.aclose()
    assert out.sweep_hits_total == 1
    await client.aclose()


# ---- Stage 6 segregated signal ----------------------------------------------


def hydrated(gid: str = "G1", funder: str = "Dept of X", net: int = 100) -> AwardRecord:
    return AwardRecord(
        gid=gid, funding_agency=funder, awarding_agency=funder,
        lifetime_obligated=Decimal("1000"),
        window=WindowAccounting(window_positive_obligations=Decimal(str(net)),
                                window_negative_deobligations=Decimal("0"),
                                window_net_obligations=Decimal(str(net)),
                                window_transaction_count=1,
                                window_activity_status="POSITIVE_ONLY",
                                transaction_population_complete=True))


def test_agency_fallback_chain():
    assert resolve_subaward_agency(awarding="A", funding="")[0] == "A"
    assert resolve_subaward_agency(awarding="", funding="B") == (
        "B", "awarding agency not reported by source; cohort credited to funding agency")
    agency, note = resolve_subaward_agency(awarding="", funding="")
    assert agency == "Unresolved"
    assert "not reported by source" in note


def test_subaward_cohort_rows_one_per_agency_and_at08_clean():
    hits = [{"sub_award_id": "S1", "prime_gid": "G1",
             "amount": Decimal("100"), "awarding_agency": "Dept of Energy",
             "funding_agency": ""},
            {"sub_award_id": "S2", "prime_gid": "G2",
             "amount": Decimal("50"), "awarding_agency": "",
             "funding_agency": "Dept of Defense"},
            {"sub_award_id": "S3", "prime_gid": "",
             "amount": None, "awarding_agency": "", "funding_agency": ""}]
    rows = build_subaward_cohort_rows(hits)
    by_agency = {r.agency: r for r in rows}
    assert set(by_agency) == {"Dept of Energy", "Dept of Defense", "Unresolved"}
    for row in rows:
        assert row.display == SUBAWARD_DISPLAY == "Tier-1A Subaward"
        assert row.award_count >= 1
        assert row.proof_gids, "phantom row: no proof"
        assert row.window_net == row.window_positive + row.window_negative
    assert by_agency["Dept of Energy"].window_net == Decimal("100")
    assert by_agency["Unresolved"].award_count == 1
    assert by_agency["Unresolved"].source_note != ""


def test_no_double_count_prime_totals_untouched():
    records = [hydrated("G1", net=100), hydrated("G2", net=200)]
    adjs = {"G1": Adjudication(gid="G1", tier="Tier-1A", display="Tier-1A", rule="KEEP"),
            "G2": Adjudication(gid="G2", tier="Tier-1A", display="Tier-1A", rule="KEEP")}
    subs = [{"sub_award_id": "S1", "prime_gid": "G1", "amount": Decimal("10000"),
             "awarding_agency": "Dept of Energy", "funding_agency": ""}]
    prime_only = build_matrix(records, adjs)
    with_subs = build_matrix(records, adjs, subawards=subs)
    assert with_subs.totals == prime_only.totals == {"Tier-1A": "300"}
    assert [(r.agency, r.window_net) for r in with_subs.matrix] == [
        (r.agency, r.window_net) for r in prime_only.matrix]
    assert with_subs.matrix_hash == prime_only.matrix_hash
    assert len(with_subs.subaward_cohorts) == 1
    assert with_subs.subaward_cohorts[0].window_net == Decimal("10000")


# ---- Renderer: banner + section 5/1 ------------------------------------------


def _audit(**kw: Any) -> dict:
    cert = issue_certificate(target_name="Example", window_start="2023-10-01",
                             window_end="2026-09-30", manifest_hash="mh",
                             matrix_hash="mx", gate_results=[], totals={})
    base = {"overall": "PASS", "status": "COMPLETE",
            "matrix": {"matrix": [], "servicing_ledger": [], "subaward_cohorts": [],
                       "totals": {}, "completeness": {}},
            "ledger": [], "subawards": {"kept_hits": [], "sweep_warning": None},
            "gates": [], "certificate": cert.model_dump(mode="json")}
    base.update(kw)
    return base


def _triage() -> dict:
    return {"status": "READY",
            "manifest": {"target_archetype": "ARCHETYPE_1", "is_conglomerate": False,
                         "term_scores": [], "rejected_terms": []}}


def test_parse_at10_detail():
    mod = _renderer()
    parsed = mod.parse_at10_detail(
        "embargo_cutoff=2026-08-03 banner_needed=True recompete_watch=none")
    assert parsed == {"embargo_cutoff": "2026-08-03", "banner_needed": True,
                      "recompete_watch": "none"}
    assert mod.parse_at10_detail(
        "embargo_cutoff=2026-08-03 banner_needed=False recompete_watch=none"
    )["banner_needed"] is False
    assert mod.parse_at10_detail(None)["banner_needed"] is False
    assert mod.parse_at10_detail("garbage")["banner_needed"] is False


def test_banner_rendered_when_needed_and_absent_otherwise():
    mod = _renderer()
    with_banner = _audit(gates=[{
        "gate_id": "AT-10", "status": "PASS",
        "detail": "embargo_cutoff=2026-08-03 banner_needed=True recompete_watch=none"}])
    report = mod.render_report("Example", "2023-10-01", "2026-09-30",
                               _triage(), with_banner)
    assert "EMBARGO / STALENESS NOTICE" in report
    assert "2026-08-03" in report
    assert report.index("EMBARGO") < report.index("## §0")
    without = _audit(gates=[{
        "gate_id": "AT-10", "status": "PASS",
        "detail": "embargo_cutoff=2026-08-03 banner_needed=False recompete_watch=none"}])
    assert "EMBARGO" not in mod.render_report("Example", "2023-10-01",
                                               "2026-09-30", _triage(), without)


def test_section5_zero_blanks_and_section1_subaward_rows():
    mod = _renderer()
    audit = _audit(
        matrix={"matrix": [], "servicing_ledger": [],
                "subaward_cohorts": [
                    {"agency": "Dept of Energy", "display": "Tier-1A Subaward",
                     "award_count": 1, "window_net": "137297.98",
                     "lifetime_total": "137297.98",
                     "proof_gids": ["CONT_AWD_EXAMPLE_GID"],
                     "proof_truncated": False,
                     "source_note": "funding agency not reported by source"}],
                "totals": {}, "completeness": {}},
        subawards={"kept_hits": [
            {"sub_award_id": "609261", "subawardee_name": "", "amount": None,
             "prime_gid": "", "prime_award_id": "", "tier": "Tier-1A",
             "awarding_agency": "", "funding_agency": "",
             "description": ""}],
            "sweep_warning": None})
    report = mod.render_report("Example", "2023-10-01", "2026-09-30",
                               _triage(), audit)
    assert "Tier-1A Subaward" in report
    assert "never added to prime totals" in report
    sec5 = report.split("## §5")[1].split("## §6")[0]
    data = [ln for ln in sec5.splitlines()
            if ln.startswith("| ") and "sub-award" not in ln and "---" not in ln]
    assert data, "expected at least one §5 data row"
    for ln in data:
        cells = list(ln.split("|")[1:-1])
        assert all(c.strip() for c in cells), f"blank cell in: {ln}"
    assert mod.NOT_REPORTED in sec5
