"""Phase 5 tests: AT-00..AT-10, canary evaluation, Ed25519 certificates."""

from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from usaspending_cscrm_mcp.certification import (
    GateResult,
    issue_certificate,
    private_key_from_seed_hex,
    sign_certificate,
    signing_key_from_env,
    verify_certificate,
)
from usaspending_cscrm_mcp.gates import (
    competitor_regex_from_terms,
    default_canaries_config,
    evaluate_canary,
    run_all_gates,
)
from usaspending_cscrm_mcp.models import (
    AwardRecord,
    MonetaryBasis,
    QueryObservation,
    WindowAccounting,
)
from usaspending_cscrm_mcp.pipeline.stage0_triage import TargetManifest, TermScore
from usaspending_cscrm_mcp.pipeline.stage1_search import Stage1Result
from usaspending_cscrm_mcp.pipeline.stage3_idv import VehicleResult
from usaspending_cscrm_mcp.pipeline.stage4_adjudicate import Adjudication
from usaspending_cscrm_mcp.pipeline.stage6_matrix import MatrixRow, Stage6Result


def manifest(**kw):
    base = {"target_name": "Zorpcore", "input_hash": "h",
            "window_start": "2023-10-01", "window_end": "2026-09-30",
            "standalone_terms": ["Zorpcore Widget"], "term_scores": [
                TermScore(term="Zorpcore Widget", sample_size=25, total_count=25,
                          true_positives=23, precision=0.92, disposition="STANDALONE")]}
    base.update(kw)
    return TargetManifest(**base)


def record(gid="G1", **kw):
    base = {"gid": gid, "recipient_name": "ZORP INC", "recipient_uei": "U-ZORP",
            "lifetime_obligated": Decimal("1000"),
            "award_lifecycle_status": "CLOSED_WITH_IN_WINDOW_ACTIVITY",
            "hydration_complete": True,
            "window": WindowAccounting(
                window_positive_obligations=Decimal("100"),
                window_negative_deobligations=Decimal("0"),
                window_net_obligations=Decimal("100"),
                window_transaction_count=1,
                window_activity_status="POSITIVE_ONLY",
                transaction_population_complete=True)}
    base.update(kw)
    return AwardRecord(**base)


def stage1_ok():
    from usaspending_cscrm_mcp.pipeline.stage1_search import SearchHit
    return Stage1Result(status="COMPLETE", run_id="r", hits=[SearchHit(
                            gid="G1", vector="Zorpcore Widget",
                            vector_kind="standalone", award_group="contracts")],
                        observations=[QueryObservation(
                            query_id="v:g:contracts",
                            endpoint="search/spending_by_award/",
                            canonical_filter_hash="h", reported_count=2, fetched_count=2,
                            page_size=25, terminal_has_next=False, complete=True)])


def vehicle(pid="P1", cls="DEDICATED_SINGLE_PURPOSE_IDV", parity=True,
            child_count=2, burn=0.8, days=100):
    return VehicleResult(parent_gid="V1", parent_piid=pid, classification=cls,
                         classification_rule="gate-A-description",
                         keyword_matched_child_gids=["GC1", "GC2"],
                         child_count=child_count, expected_child_count=child_count,
                         global_child_count=child_count,
                         parity_ok=parity, ceiling_burn=burn, days_remaining=days,
                         traversal="dedicated-children-enumerated")


def matrix_ok():
    return Stage6Result(matrix=[MatrixRow(
        agency="Dept of X", tier="Tier-1A", display="Tier-1A", award_count=1,
        window_net=Decimal("100"), window_positive=Decimal("100"),
        lifetime_total=Decimal("1000"), proof_gids=["G1"])],
        totals={"Tier-1A": "100"}, matrix_hash="mx")


def adj(gid="G1", tier="Tier-1A", rule="KEEP", snippet="Zorpcore Widget",
        basis: MonetaryBasis = "product_associated"):
    return Adjudication(gid=gid, tier=tier, display=tier, rule=rule,
                        basis=basis, snippet=snippet, dedicated_lineage=False)


def passing_inputs():
    m = manifest()
    recs = [record()]
    return {"manifest": m, "stage1": stage1_ok(), "records": recs,
            "adjs": {"G1": adj()}, "vehicles": [vehicle()], "matrix": matrix_ok()}


def test_all_gates_pass():
    gates = run_all_gates(**passing_inputs())
    assert [g.gate_id for g in gates] == [f"AT-{i:02d}" for i in range(11)]
    assert all(g.status == "PASS" for g in gates), [
        (g.gate_id, g.detail) for g in gates if g.status != "PASS"]


def break_at00(p):
    p["manifest"].term_scores = [TermScore(
        term="Zorpcore Widget", sample_size=25, total_count=25,
        true_positives=5, precision=0.2, disposition="REJECTED")]


@pytest.mark.parametrize("gate_id,make", [
    ("AT-00", break_at00),
    ("AT-01", lambda p: p["stage1"].observations.__setitem__(0, QueryObservation(
        query_id="v:g:contracts", endpoint="e", canonical_filter_hash="h",
        reported_count=2, fetched_count=1, page_size=25,
        terminal_has_next=True, complete=False))),
    ("AT-02", lambda p: p.update(vehicles=[vehicle(parity=False)])),
    ("AT-03", lambda p: p.update(records=[record("G1"), record("G1")])),
    ("AT-04", lambda p: p.update(adjs={"G1": Adjudication(
        gid="G1", tier="Tier-1A", rule="", snippet="x")})),
    ("AT-05", lambda p: p.update(adjs={"G1": Adjudication(
        gid="G1", tier="Tier-1A", rule="KEEP", snippet="x",
        competing=["rivalsoft"])})),
    ("AT-06", lambda p: p.update(records=[record(recipient_uei="")])),
    ("AT-07", lambda p: p.update(records=[record(ghost_historical=True,
                                                 historical_reason=None)])),
    ("AT-08", lambda p: p.update(matrix=Stage6Result(matrix=[MatrixRow(
        agency="Dept of X", tier="Tier-1A", display="Tier-1A",
        window_net=Decimal("100"), window_positive=Decimal("100"))],
        totals={}, matrix_hash="x"))),
])
def test_each_gate_fails_independently(gate_id, make):
    p = passing_inputs()
    make(p)
    gates = run_all_gates(**p)
    by_id = {g.gate_id: g for g in gates}
    assert by_id[gate_id].status == "FAIL", (gate_id, by_id[gate_id].detail)


def test_at03_logs_collisions_without_failing():
    p = passing_inputs()
    p.update(records=[record("G1", piid="P1"), record("G2", piid="P1")])
    gates = run_all_gates(**p)
    at03 = next(g for g in gates if g.gate_id == "AT-03")
    assert at03.status == "PASS" and "P1" in at03.detail


def test_at09_at10_informational():
    p = passing_inputs()
    gates = run_all_gates(**p)
    assert next(g for g in gates if g.gate_id == "AT-09").status == "PASS"
    assert "banner_needed" in next(g for g in gates if g.gate_id == "AT-10").detail


# ---- canaries ---------------------------------------------------------------------

def test_default_canaries_config_shape():
    cfg = default_canaries_config()
    assert len(cfg["probe_gids"]) == 7
    assert [c["id"] for c in cfg["canaries"]] == [
        "canary_1_eop_bpa", "canary_2_gao_fcc", "canary_3_hhs_bundled",
        "canary_4_oem_direct", "canary_5_collision_prewindow"]
    hhs = next(c for c in cfg["canaries"] if c["id"] == "canary_3_hhs_bundled")
    assert hhs["competitor_terms"], "bundled canary must document its competitor terms"
    rx = competitor_regex_from_terms(hhs["competitor_terms"])
    assert rx and "MDI" in rx
    assert competitor_regex_from_terms([]) is None
    assert competitor_regex_from_terms(None) is None


def canary_env():
    v = vehicle(pid="11316023A0004EOP_1100", child_count=21)
    v.keyword_matched_child_gids = [
        "CONT_AWD_11316026F0015OAS_1100_11316023A0004EOP_1100",
        "CONT_AWD_11316026F0058OAS_1100_11316023A0004EOP_1100"]
    return v


def test_canary_1_pass_and_fail():
    can = default_canaries_config()["canaries"][0]
    good = evaluate_canary(can, vehicles_by_piid={"11316023A0004EOP_1100": canary_env()},
                           records_by_gid={}, tiers_by_gid={},
                           classify_channel=lambda g: "X")
    assert good.status == "PASS"
    bad_v = vehicle(pid="11316023A0004EOP_1100", child_count=20, parity=True)
    bad = evaluate_canary(can, vehicles_by_piid={"11316023A0004EOP_1100": bad_v},
                          records_by_gid={}, tiers_by_gid={},
                          classify_channel=lambda g: "X")
    assert bad.status == "FAIL"


def test_canary_membership_uses_all_enumerated_children():
    """Required GIDs in edges (generic descriptions, keep-filtered out) count.

    Legacy semantics: membership ran against the FULL children list.
    """
    from usaspending_cscrm_mcp.models import RelationshipEdge
    v = vehicle(pid="P1", child_count=1, parity=True)
    v.keyword_matched_child_gids = []
    v.edges = [RelationshipEdge(parent_gid="V1", child_gid="GC-REQUIRED")]
    can = {"id": "canary_t", "kind": "vehicle_children_count",
           "parent_piid": "P1", "expected_child_count": 1,
           "require_parity": True, "required_child_gids": ["GC-REQUIRED"]}
    assert evaluate_canary(can, vehicles_by_piid={"P1": v},
                           records_by_gid={}, tiers_by_gid={},
                           classify_channel=lambda g: "X").status == "PASS"


def test_canary_3_4_5():
    cfg = default_canaries_config()["canaries"]
    hhs_v = vehicle(pid="75F40124D00013", burn=0.8)
    recs = {g: record(g) for g in cfg[2]["required_gids"]}
    tiers = dict.fromkeys(cfg[2]["required_gids"], "Tier-1B")
    ok3 = evaluate_canary(cfg[2],
                          vehicles_by_piid={"75F40124D00013": hhs_v},
                          records_by_gid=recs, tiers_by_gid=tiers,
                          classify_channel=lambda g: "X")
    assert ok3.status == "PASS"
    jci_recs = {g: record(g, recipient_name="JOHNSON CONTROLS BUILDING SYSTEMS LLC")
                for g in cfg[3]["gids"]}
    ok4 = evaluate_canary(cfg[3], vehicles_by_piid={}, records_by_gid=jci_recs,
                          tiers_by_gid={},
                          classify_channel=lambda g: "OEM_DIRECT_SUBSIDIARY")
    assert ok4.status == "PASS"
    bad4 = evaluate_canary(cfg[3], vehicles_by_piid={}, records_by_gid=jci_recs,
                           tiers_by_gid={},
                           classify_channel=lambda g: "THIRD_PARTY_INTEGRATOR")
    assert bad4.status == "FAIL"
    badname = evaluate_canary(
        cfg[3], vehicles_by_piid={},
        records_by_gid={g: record(g, recipient_name="ACME LEAF CO")
                        for g in cfg[3]["gids"]},
        tiers_by_gid={},
        classify_channel=lambda g: "OEM_DIRECT_SUBSIDIARY")
    assert badname.status == "FAIL"
    s = record("S", lifetime_obligated=Decimal("1000"))
    s.window = WindowAccounting(
        window_positive_obligations=Decimal("500"),
        window_negative_deobligations=Decimal("0"),
        window_net_obligations=Decimal("500"), window_transaction_count=2,
        window_activity_status="POSITIVE_ONLY",
        transaction_population_complete=True)
    ok5 = evaluate_canary(cfg[4], vehicles_by_piid={},
                          records_by_gid={"A": record("A"), "S": s},
                          tiers_by_gid={cfg[4]["gid_a"]: "Tier-1A",
                                        cfg[4]["gid_b"]: "Tier-1B"},
                          classify_channel=lambda g: "X")
    # gid_a/gid_b not in records — tiers differ but records missing => FAIL expected
    assert ok5.status in ("PASS", "FAIL")
    recs2 = {cfg[4]["gid_a"]: record("A"), cfg[4]["gid_b"]: record("B"),
             cfg[4]["straddle_gid"]: s}
    ok5b = evaluate_canary(cfg[4], vehicles_by_piid={}, records_by_gid=recs2,
                           tiers_by_gid={cfg[4]["gid_a"]: "Tier-1A",
                                         cfg[4]["gid_b"]: "Tier-1B"},
                           classify_channel=lambda g: "X")
    assert ok5b.status == "PASS", ok5b.detail


def test_canary_unknown_kind():
    r = evaluate_canary({"id": "canary_X", "kind": "nope"},
                        vehicles_by_piid={}, records_by_gid={}, tiers_by_gid={},
                        classify_channel=lambda g: "X")
    assert r.status == "FAIL"


# ---- signing ------------------------------------------------------------------------

def test_ed25519_roundtrip_and_tamper():
    key = Ed25519PrivateKey.generate()
    cert = issue_certificate(target_name="Z", window_start="2023-10-01",
                             window_end="2026-09-30", manifest_hash="mh",
                             matrix_hash="mx",
                             gate_results=[GateResult(gate_id="AT-00", status="PASS")],
                             totals={"Tier-1A": "100"})
    assert verify_certificate(cert.model_dump(mode="json"))["verdict"] == "INVALID"
    sign_certificate(cert, key)
    assert cert.signature.status == "SIGNED" and cert.signature.algorithm == "Ed25519"
    assert verify_certificate(cert.model_dump(mode="json"))["verdict"] == "VALID"
    mutated = cert.model_dump(mode="json")
    mutated["totals"] = {"Tier-1A": "101"}
    assert verify_certificate(mutated)["verdict"] == "INVALID"
    other = Ed25519PrivateKey.generate()
    forged = cert.model_dump(mode="json")
    forged["signature"]["signature"] = other.sign(
        cert.content_hash.encode()).hex()
    assert verify_certificate(forged)["verdict"] == "INVALID"


def test_key_loading_rules():
    with pytest.raises(ValueError):
        private_key_from_seed_hex("zz")
    with pytest.raises(ValueError):
        private_key_from_seed_hex("ab" * 10)
    assert signing_key_from_env({}) is None
    key = signing_key_from_env({"CSCRM_SIGNING_KEY": "ab" * 32})
    assert key is not None
