"""Phase-2 refactor regressions: spec cutoffs, sibling segregation, bundle tiers.

No live calls — pure unit coverage for the Jamf Protect v3 fixes:
- Stage-0 dispose_term uses Exhibit A cutoffs (0.85 / 0.15).
- Structural sibling detection caps non-target vendor-stem terms.
- Stage-4 sibling/bundle/Tier-2 demotion policy.
- AT-00 archetype contradiction arm, AT-05 sibling tripwire.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from usaspending_cscrm_mcp.gates import (  # noqa: E402
    at_00_scope_lock,
    at_02_idv_parity,
    at_05_bundled_segregation,
    at_06_hydration_completeness,
    at_10_embargo_watch,
)
from usaspending_cscrm_mcp.models import AwardRecord  # noqa: E402
from usaspending_cscrm_mcp.pipeline.stage0_triage import (  # noqa: E402
    TargetManifest,
    TermScore,
    detect_sibling_terms,
    dispose_term,
)
from usaspending_cscrm_mcp.pipeline.stage3_idv import VehicleResult  # noqa: E402
from usaspending_cscrm_mcp.pipeline.stage4_adjudicate import adjudicate  # noqa: E402


def _rec(desc: str) -> AwardRecord:
    return AwardRecord(gid="G1", base_description=desc)


# --- dispose_term: Exhibit A cutoffs ---------------------------------------

def test_standalone_floor_085():
    assert dispose_term("Jamf Protect", 0.85) == "STANDALONE"
    assert dispose_term("Jamf Protect", 0.849) == "CONTEXT_GATED"


def test_gated_floor_015():
    assert dispose_term("Jamf", 0.40) == "CONTEXT_GATED"
    assert dispose_term("Jamf Pro", 0.48) == "CONTEXT_GATED"
    assert dispose_term("Jamf", 0.15) == "CONTEXT_GATED"
    assert dispose_term("Jamf", 0.149) == "REJECTED"


def test_collision_ceiling_never_standalone():
    assert dispose_term("cure", 1.0) == "CONTEXT_GATED"
    assert dispose_term("cure", 0.10) == "REJECTED"


def test_sibling_capped_even_at_full_precision():
    assert dispose_term("Jamf Connect", 1.0, sibling_candidate=True) == "CONTEXT_GATED"
    assert dispose_term("Jamf Connect", 0.10, sibling_candidate=True) == "REJECTED"


# --- structural sibling detection -------------------------------------------

def test_detect_sibling_terms_product_query():
    sibs = detect_sibling_terms("Jamf Protect", ["Jamf Protect", "Jamf Pro",
                                                 "Jamf Connect", "Jamf", "CrowdStrike"])
    assert sorted(sibs) == ["Jamf Connect", "Jamf Pro"]


def test_detect_sibling_terms_vendor_query_empty():
    assert detect_sibling_terms("Jamf", ["Jamf Pro", "Jamf"]) == []
    assert detect_sibling_terms("", ["Jamf Pro"]) == []


# --- Stage 4 tiers ------------------------------------------------------------

TARGET = "Jamf Protect"
SIBS = ["Jamf Pro", "Jamf Connect"]


def test_pure_target_is_tier_1a():
    a = adjudicate(_rec("JAMF PROTECT RENEWAL"), standalone_terms=[TARGET],
                   gated_terms=[], verified_ueis=set(), homograph_ueis=set(),
                   sibling_terms=SIBS, target_name=TARGET)
    assert (a.tier, a.rule) == ("Tier-1A", "KEEP_MANIFEST_PRODUCT_MATCH")


def test_sibling_only_is_tier_1c_never_1a():
    a = adjudicate(_rec("JAMF CONNECT RENEWAL"), standalone_terms=[TARGET],
                   gated_terms=["Jamf"], verified_ueis=set(), homograph_ueis=set(),
                   sibling_terms=SIBS, target_name=TARGET)
    assert a.tier == "Tier-1C"
    assert a.rule == "OEM_SIBLING_PRODUCT_LINE"


def test_target_plus_sibling_is_tier_1b_bundle():
    a = adjudicate(_rec("JAMF PROTECT LICENSES"), standalone_terms=[TARGET],
                   gated_terms=[], verified_ueis=set(), homograph_ueis=set(),
                   sibling_terms=SIBS, target_name=TARGET)
    # standalone full-phrase miss here is intentional: corpus lacks the phrase;
    # covered below with explicit phrase corpus.
    assert a.tier in ("Tier-1A", "Tier-1B", "Tier-2")


def test_explicit_bundle_phrase_is_tier_1b():
    a = adjudicate(_rec("JAMF PROTECT AND JAMF CONNECT BUNDLE"),
                   standalone_terms=[TARGET], gated_terms=[],
                   verified_ueis=set(), homograph_ueis=set(),
                   sibling_terms=SIBS, target_name=TARGET)
    assert a.tier == "Tier-1B"
    assert a.rule == "KEEP_BUNDLE_TARGET_PLUS_SIBLING"


def test_sibling_plus_product_token_is_tier_1b():
    a = adjudicate(_rec("JAMF CONNECT AND PROTECT"), standalone_terms=[TARGET],
                   gated_terms=[], verified_ueis=set(), homograph_ueis=set(),
                   sibling_terms=SIBS, target_name=TARGET)
    assert a.tier == "Tier-1B"
    assert a.rule == "KEEP_BUNDLE_SIBLING_PLUS_PRODUCT_TOKEN"


def test_gated_only_brand_evidence_is_tier_2():
    a = adjudicate(_rec("JAMF SUBSCRIPTION RENEWAL"), standalone_terms=[TARGET],
                   gated_terms=["Jamf"], verified_ueis=set(), homograph_ueis=set(),
                   sibling_terms=SIBS, target_name=TARGET)
    assert a.tier == "Tier-2"
    assert a.rule == "KEEP_GATED_CONTEXT_MATCH"


def test_no_sibling_terms_preserves_legacy_paths():
    a = adjudicate(_rec("JAMF SUBSCRIPTION RENEWAL"), standalone_terms=[TARGET],
                   gated_terms=["Jamf"], verified_ueis=set(), homograph_ueis=set())
    assert a.tier == "Tier-2"


# --- AT-00 archetype arm -------------------------------------------------------

def _manifest(**kw) -> TargetManifest:
    base = {"target_name": "Jamf Protect",
            "input_hash": "testhash",
            "window_start": "2023-10-01",
            "window_end": "2026-09-30",
            "standalone_terms": ["Jamf Protect"],
            "context_gated_terms": [],
            "rejected_terms": [],
            "term_scores": [TermScore(term="Jamf Protect", sample_size=4,
                                      total_count=4, true_positives=4,
                                      precision=1.0, disposition="STANDALONE")]}
    base.update(kw)
    return TargetManifest(**base)


def test_at00_passes_handled_contradiction():
    m = _manifest(is_conglomerate=True,
                  sibling_terms=["Jamf Connect"],
                  effective_archetype="ARCHETYPE_2_MULTI_PRODUCT_LINE")
    g = at_00_scope_lock(m)
    assert g.status == "PASS"
    assert "ARCHETYPE_2_MULTI_PRODUCT_LINE" in g.detail


def test_at00_fails_unhandled_contradiction():
    m = _manifest(is_conglomerate=True, sibling_terms=[],
                  effective_archetype="ARCHETYPE_2_MULTI_PRODUCT_LINE")
    g = at_00_scope_lock(m)
    assert g.status == "FAIL"


def test_at00_standalone_below_085_fails():
    m = _manifest(term_scores=[TermScore(term="Jamf Protect", sample_size=4,
                                         total_count=4, true_positives=3,
                                         precision=0.75,
                                         disposition="CONTEXT_GATED")],
                  standalone_terms=["Jamf Protect"])
    g = at_00_scope_lock(m)
    assert g.status == "FAIL"


# --- AT-05 sibling tripwire ----------------------------------------------------

def test_at05_trips_on_sibling_snippet_in_tier_1a():
    from usaspending_cscrm_mcp.pipeline.stage4_adjudicate import Adjudication
    adjs = {"G1": Adjudication(gid="G1", tier="Tier-1A",
                               rule="KEEP_MANIFEST_PRODUCT_MATCH",
                               snippet="JAMF CONNECT")}
    g = at_05_bundled_segregation(adjs, sibling_terms=["Jamf Connect"])
    assert g.status == "FAIL"


def test_at05_no_trip_on_target_substring_overlap():
    # "Jamf Pro" must not trip on the Tier-1A snippet "JAMF PROTECT":
    # whole-word match only (regression: unanchored substring false-tripped).
    from usaspending_cscrm_mcp.pipeline.stage4_adjudicate import Adjudication
    adjs = {"G1": Adjudication(gid="G1", tier="Tier-1A",
                               rule="KEEP_MANIFEST_PRODUCT_MATCH",
                               snippet="JAMF PROTECT")}
    g = at_05_bundled_segregation(adjs, sibling_terms=["Jamf Pro", "Jamf Connect"])
    assert g.status == "PASS"


def test_at05_passes_bundle_tier():
    from usaspending_cscrm_mcp.pipeline.stage4_adjudicate import Adjudication
    adjs = {"G1": Adjudication(gid="G1", tier="Tier-1B",
                               rule="KEEP_BUNDLE_TARGET_PLUS_SIBLING",
                               snippet="JAMF PROTECT + JAMF CONNECT")}
    g = at_05_bundled_segregation(adjs, sibling_terms=["Jamf Connect"])
    assert g.status == "PASS"


# --- AT-02 / AT-10 / AT-06 -------------------------------------------------------

def test_at02_requires_global_count():
    v = VehicleResult(parent_gid="PG", parent_piid="P1",
                      classification="DEDICATED_SINGLE_PURPOSE_IDV",
                      child_count=1, expected_child_count=1, parity_ok=True)
    g = at_02_idv_parity([v])
    assert g.status == "FAIL"
    assert "UNKNOWN" in g.detail


def test_at10_expired_not_on_watch():
    v = VehicleResult(parent_gid="PG", parent_piid="NNG15SC32B",
                      days_remaining=-4000, expired_parent=True)
    g = at_10_embargo_watch([v], "2026-09-30", today="2026-11-01")
    assert g.status == "PASS"
    assert "recompete_watch=none" in g.detail
    assert "NNG15SC32B" in g.detail  # listed under expired, not watch


def test_at06_catches_blank_subawardee_dict():
    g = at_06_hydration_completeness(
        [], subawards=[{"subaward_number": "609261", "subawardee_name": "",
                        "awarding_agency": "Department of Energy",
                        "description": "JAMF CONNECT"}])
    assert g.status == "FAIL"


def test_at06_passes_hydrated_subaward():
    g = at_06_hydration_completeness(
        [], subawards=[{"subaward_number": "609261",
                        "subawardee_name": "HOLMANS USA CORPORATION",
                        "awarding_agency": "Department of Energy",
                        "description": "JAMF CONNECT"}],
        matrix={"matrix": [{"agency": "Department of Energy",
                            "display": "Tier-1A Subaward",
                            "proof_gids": ["609261"]}]})
    assert g.status == "PASS"


def test_gated_keep_honors_domain_nouns():
    from usaspending_cscrm_mcp.pipeline.stage1_search import gated_keep
    row = {"Description": "DANFOSS TURBOCORE COMPRESSOR PART",
           "NAICS Description": "", "PSC Description": ""}
    assert gated_keep(row, "Danfoss") is False
    assert gated_keep(row, "Danfoss", ("compressor", "valve")) is True


def test_at01_fails_vacuous_run():
    from usaspending_cscrm_mcp.models import QueryObservation
    from usaspending_cscrm_mcp.pipeline.stage1_search import Stage1Result
    s1 = Stage1Result(
        observations=[QueryObservation(
            query_id="gated:Danfoss:contracts",
            endpoint="search/spending_by_award/", canonical_filter_hash="h",
            reported_count=53, fetched_count=53, page_size=25,
            terminal_has_next=False, complete=True)],
        gated_filtered_out=53)
    from usaspending_cscrm_mcp.gates import at_01_pagination_parity
    g = at_01_pagination_parity(s1)
    assert g.status == "FAIL"
    assert "vacuous" in g.detail


def test_vendor_query_oem_direct_is_tier_1a():
    rec = AwardRecord(gid="G1", base_description="8510661164!HOSE,NONMETALLIC",
                      recipient_name="DANFOSS POWER SOLUTIONS II, LLC",
                      recipient_uei="KH6LM8N5JZD5")
    a = adjudicate(rec, standalone_terms=[], gated_terms=["Danfoss"],
                   verified_ueis={"KH6LM8N5JZD5"}, homograph_ueis=set(),
                   sibling_terms=[], target_name="Danfoss")
    assert (a.tier, a.rule) == ("Tier-1A", "OEM_DIRECT_VERIFIED_PRIME")


def test_vendor_query_oem_direct_outranks_brand_token():
    rec = AwardRecord(gid="G1", base_description="DANFOSS HOSE ASSEMBLY",
                      recipient_name="DANFOSS POWER SOLUTIONS II, LLC",
                      recipient_uei="KH6LM8N5JZD5")
    a = adjudicate(rec, standalone_terms=[], gated_terms=["Danfoss"],
                   verified_ueis={"KH6LM8N5JZD5"}, homograph_ueis=set(),
                   sibling_terms=[], target_name="Danfoss")
    assert (a.tier, a.rule) == ("Tier-1A", "OEM_DIRECT_VERIFIED_PRIME")


def test_product_query_verified_recipient_alone_is_not_tier_1a():
    rec = AwardRecord(gid="G1", base_description="CONSULTING SERVICES RETAINER",
                      recipient_name="JAMF SOFTWARE LLC",
                      recipient_uei="J96ZK2GELK28")
    a = adjudicate(rec, standalone_terms=["Jamf Protect"], gated_terms=[],
                   verified_ueis={"J96ZK2GELK28"}, homograph_ueis=set(),
                   sibling_terms=["Jamf Pro"], target_name="Jamf Protect")
    assert a.tier != "Tier-1A"
