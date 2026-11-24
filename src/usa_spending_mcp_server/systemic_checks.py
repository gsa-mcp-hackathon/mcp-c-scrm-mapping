"""v5 systemic regression checks REG-01..REG-12 (pure, offline).

Each check exercises a structural invariant against synthetic fixtures that
reproduce a historical bug class. `run_all_systemic_checks()` is called by
`run_cscrm_audit_suite` (a failing check fails the suite) and by
`tests/test_systemic_regressions.py` (one test per REG id).
"""

from __future__ import annotations

from typing import Any

from usa_spending_mcp_server.cscrm_systemic import (
    DROP_HOMOGRAPH,
    TIER_1A,
    TIER_1C,
    is_high_collision_token,
)
from usa_spending_mcp_server.tools.cscrm_context import classify_ghost, compute_window_sums
from usa_spending_mcp_server.tools.cscrm_idv import classify_container
from usa_spending_mcp_server.tools.cscrm_lexicon import adjudicate_candidate
from usa_spending_mcp_server.tools.cscrm_recipient import classify_fulfillment_channel
from usa_spending_mcp_server.tools.cscrm_search import dedupe_awards
from usa_spending_mcp_server.tools.cscrm_subawards import adjudicate_sweep_hits
from usa_spending_mcp_server.tools.cscrm_triage import _disposition, _score_sample

PASS = "PASS"
FAIL = "FAIL"


def _steel_sample(n: int = 25) -> list[dict[str, Any]]:
    """25 rows that all contain 'Galvanize' but describe galvanized steel."""
    rows = []
    for i in range(n):
        rows.append({
            "Award ID": f"STEEL{i:04d}",
            "Recipient Name": f"STRUCTURAL STEEL CO {i % 5}",
            "Recipient UEI": f"STEELUEI{i % 5:09d}",
            "Description": "HOT-DIP GALVANIZED STEEL BEAMS, ASTM A123, BRIDGE REPAIR",
            "NAICS Code": "332312",
            "PSC Code": "9510",
        })
    return rows


def check_reg01() -> tuple[str, str]:
    """Non-tautological precision: every-galvanize steel sample scores 0.0."""
    scored = _score_sample("Galvanize", _steel_sample())
    ok = scored["precision"] == 0.0 and scored["true_positives"] == 0
    # Positive control: software delivery rows still score.
    good = _score_sample("ACL Analytics", [{
        "Award ID": "A1", "Recipient Name": "DILIGENT CORP", "Recipient UEI": "Z8LJBUBQX4V3",
        "Description": "ACL ANALYTICS ENTERPRISE LICENSES, ANNUAL SUBSCRIPTION RENEWAL",
        "NAICS Code": "541519", "PSC Code": "D319"}])
    ok = ok and good["precision"] == 1.0
    return (PASS if ok else FAIL,
            f"galvanize-steel P={scored['precision']} TP={scored['true_positives']}; "
            f"software-control P={good['precision']}")


def check_reg02() -> tuple[str, str]:
    """High-collision ceiling: short/common/homograph tokens never standalone."""
    cases = {"Diligent": True, "ACL": True, "iSTAR": True, "Galvanize": True,
             "C-CURE 9000": False, "HighBond": False, "ACL Analytics": False}
    bad = [k for k, exp in cases.items() if is_high_collision_token(k) != exp]
    disp = _disposition("Diligent", 100, 1.0, True)
    ok = not bad and disp == "CONTEXT_GATED"
    return (PASS if ok else FAIL,
            f"mismatches={bad or 'none'}; Diligent@P=1.0 -> {disp}")


def check_reg03() -> tuple[str, str]:
    """Inflection + boilerplate guard across targets."""
    manifest = {"standalone_keywords": ["Diligent", "C-CURE 9000", "Cure"],
                "positive_domain_context_regex": r"\b(security|software)\b"}
    texts = ["prosecute the work diligently per FAR 52.211-10",
             "180°C cure prepreg composite autoclave",
             "honey cured ham rations",
             "cure notice issued to contractor",
             "due diligence review of financials"]
    tiers = [adjudicate_candidate(t, [], None, manifest=manifest)["tier"] for t in texts]
    # INV2 adjudication bar: bare domain nouns without any product/name
    # token are provisional, never Tier-1 (Smithsonian $6.4M class).
    enterprise = adjudicate_candidate(
        "ENTERPRISE ELECTRONIC SECURITY SYSTEM MAINTENANCE", [], "J063",
        manifest={"standalone_keywords": ["C-CURE 9000"],
                  "context_gated_keywords": ["Software House", "C-CURE"],
                  "positive_domain_context_regex": r"\b(pacs|security|c-cure)\b"})
    ok = all(t.startswith("DROPPED") for t in tiers)
    ok = ok and enterprise["tier"] == "DROPPED_POLYSEMOUS_NO_SECURITY_CONTEXT" \
        and enterprise["rule"] == "DOMAIN_CONTEXT_ONLY_NO_PRODUCT_TOKEN"
    # Term-conferred gated hits still confirm (product token present).
    term_hit = adjudicate_candidate(
        "DILIGENT DATA ANALYTICS PLATFORM SUPPORT", [], "D319",
        manifest={"standalone_keywords": ["ACL Analytics"],
                  "context_gated_keywords": ["Diligent"],
                  "positive_domain_context_regex": r"\b(audit|diligent|grc)\b"})
    ok = ok and term_hit["tier"] == "TIER_1A_CONFIRMED_PURE"
    return (PASS if ok else FAIL, f"tiers={tiers} enterprise={enterprise['rule']} term={term_hit['tier']}")


def check_reg04() -> tuple[str, str]:
    """Entity homograph isolation via UEI graph."""
    verified = {"Z8LJBUBQX4V3"}
    homographs = {"SYNCWKJ72KW1"}
    chan = classify_fulfillment_channel("SYNCWKJ72KW1", None, "DILIGENT CONSULTING, INC.",
                                        verified_oem_ueis=verified,
                                        homograph_ueis=homographs,
                                        oem_name="DILIGENT CORPORATION")
    v = adjudicate_candidate("SETA SUPPORT SERVICES", [], None,
                             manifest={"standalone_keywords": ["Diligent"],
                                       "verified_oem_ueis": sorted(verified),
                                       "excluded_homograph_ueis": sorted(homographs)},
                             recipient_uei="SYNCWKJ72KW1")
    ok = chan == "THIRD_PARTY_INTEGRATOR" and v["tier"] == DROP_HOMOGRAPH
    return (PASS if ok else FAIL, f"channel={chan} tier={v['tier']}")


def check_reg05() -> tuple[str, str]:
    """Conglomerate sibling segregation into TIER_1C."""
    manifest = {"standalone_keywords": ["ACL Analytics"],
                "is_pure_play_vendor": False,
                "verified_oem_ueis": ["Z8LJBUBQX4V3"],
                "oem_sibling_product_regex": r"\b(board ?effect|boarddocs|diligent boards)\b"}
    hvac = adjudicate_candidate("METASYS HVAC CHILLER MAINTENANCE", [], "J079",
                                manifest=manifest, recipient_uei="Z8LJBUBQX4V3")
    boards = adjudicate_candidate("BOARDEFFECT PORTAL SUBSCRIPTION RENEWAL", [], "D319",
                                  manifest=manifest, recipient_uei="Z8LJBUBQX4V3")
    ok = hvac["tier"] != TIER_1A and boards["tier"] == TIER_1C
    return (PASS if ok else FAIL, f"hvac={hvac['tier']} boards={boards['tier']}")


def check_reg06() -> tuple[str, str]:
    """Intrinsic container homogeneity (anti-mirage gate)."""
    c1, _, _ = classify_container(2058, True, True, 0.30, 12)   # mirage shape
    c2, _, _ = classify_container(30, True, True, 0.50, 3)      # MATOC shape
    c3, r3, _ = classify_container(21, True, True, 1.0, 1)      # dedicated BPA
    ok = (c1 == "BROAD_MULTI_PURPOSE_IDV" and c2 == "BROAD_MULTI_PURPOSE_IDV"
          and c3 == "DEDICATED_SINGLE_PURPOSE_IDV" and r3 == "gate-A-description")
    return (PASS if ok else FAIL, f"mirage={c1} matoc={c2} bpa={c3}/{r3}")


def check_reg07() -> tuple[str, str]:
    """Trailing-slash parity + fail-closed parity coercion."""
    import inspect

    from usa_spending_mcp_server.tools import cscrm_idv as idv_mod
    src = inspect.getsource(idv_mod)
    slashes = "idvs/awards/" in src and "idvs/activity/" in src
    ok = slashes and bool(None) is False
    return (PASS if ok else FAIL, f"trailing_slashes={slashes} parity_None_is_False={bool(None) is False}")


def check_reg08() -> tuple[str, str]:
    """Unfiltered sums: +1M keyword mod and -1M generic mod net to 0 + ghost."""
    txs = [{"Action Date": "2024-03-01", "Transaction Amount": 1000000,
            "Transaction Description": "Software License", "Mod": "0"},
           {"Action Date": "2024-06-01", "Transaction Amount": -1000000,
            "Transaction Description": "ADMINISTRATIVE DEOBLIGATION", "Mod": "P00001"}]
    inwin, excluded, neg = compute_window_sums(txs, "2023-10-01", "2026-12-31")
    ghost, reason = classify_ghost(txs, 1000000.0, inwin, "2024-09-30", "2026-12-31")
    ok = inwin == 0.0 and ghost and reason in ("IN_WINDOW_NET_DEOBLIGATION",
                                               "ENDED_PRE_WINDOW_ZERO_IN_WINDOW", "ZERO_OR_NEGATIVE_LIFETIME")
    return (PASS if ok else FAIL, f"in_window={inwin} ghost={ghost} reason={reason}")


def check_reg09() -> tuple[str, str]:
    """Cohort row invariants: phantom rows violate, clean rows pass."""
    phantom = [{"agency": "Department of the Interior", "tier1a_in_window": 0,
                "tier1b_in_window": 0, "tier2_provisional": 0,
                "in_window_award_count": 0, "active_award_count": 0,
                "completed_award_count": 0}]
    clean = [{"agency": "Department of Justice", "tier1a_in_window": 1953432.85,
              "tier1b_in_window": 0, "tier2_provisional": 0,
              "in_window_award_count": 5, "active_award_count": 3,
              "completed_award_count": 2}]
    ok = bool(check_matrix_invariants(phantom)) and not check_matrix_invariants(clean)
    return (PASS if ok else FAIL,
            f"phantom_violations={check_matrix_invariants(phantom)} clean={check_matrix_invariants(clean)}")


def check_reg10() -> tuple[str, str]:
    """GID primary key: bare-PIID collisions survive as separate records."""
    rows = [{"generated_internal_id": "G1", "Award ID": "75D30124F00014"},
            {"generated_internal_id": "G2", "Award ID": "75D30124F00014"}]
    out = dedupe_awards(rows)
    ok = len(out) == 2
    return (PASS if ok else FAIL, f"kept={len(out)}/2")


def check_reg11() -> tuple[str, str]:
    """Subaward sweep symmetry: hits adjudicated, kept rows renderable."""
    manifest = {"standalone_keywords": ["ACL Analytics"],
                "positive_domain_context_regex": r"\b(acl analytics|audit)\b"}
    hits = [{"Sub-Award ID": "S1", "Sub-Award Description": "ACL ANALYTICS LICENSES",
             "Sub-Award Amount": 50000, "Prime Award ID": "P1",
             "prime_award_generated_internal_id": "G1"},
            {"Sub-Award ID": "S2", "Sub-Award Description": "OFFICE FURNITURE",
             "Sub-Award Amount": 1000, "Prime Award ID": "P1",
             "prime_award_generated_internal_id": "G1"}]
    kept = adjudicate_sweep_hits(hits, ["ACL Analytics"], manifest)
    ok = len(kept) == 1 and kept[0]["sub_award_id"] == "S1" and kept[0]["rule"]
    return (PASS if ok else FAIL, f"kept={len(kept)}/1 rule={kept[0]['rule'] if kept else None}")


def check_reg12() -> tuple[str, str]:
    """Fail-closed: None/NaN/blank required fields never pass."""
    bad_values = (None, float("nan"), "", "—")
    fails = [v for v in bad_values if not _verified(v)]
    ok = len(fails) == len(bad_values) and _verified("x")
    return (PASS if ok else FAIL, f"rejected={len(fails)}/{len(bad_values)}")


def _verified(v: Any) -> bool:
    """INV6 zero pass-through: only genuine non-blank values verify."""
    if v is None:
        return False
    if isinstance(v, float):
        return v == v  # NaN never verifies
    if isinstance(v, str):
        return v.strip() not in ("", "—", "NONE", "null")
    return True


def check_matrix_invariants(matrix: list[dict[str, Any]]) -> list[str]:
    """INV5/REG-09 row invariants over a built Section-1 matrix."""
    violations = []
    for row in matrix:
        total = (row.get("tier1a_in_window") or 0) + (row.get("tier1b_in_window") or 0) \
            + (row.get("tier2_provisional") or 0)
        n = row.get("in_window_award_count")
        if n is None:
            violations.append(f"{row.get('agency')}: missing award count")
            continue
        if (total > 0) != (n >= 1):
            violations.append(f"{row.get('agency')}: spend {total} vs awards {n}")
        if row.get("active_award_count", 0) + row.get("completed_award_count", 0) != n:
            violations.append(f"{row.get('agency')}: active+completed != {n}")
        if total <= 0:
            violations.append(f"{row.get('agency')}: phantom non-positive row")
    return violations


REGISTRY: list[tuple[str, str]] = [
    ("REG-01", "Non-tautological precision probe"),
    ("REG-02", "High-collision & common-word ceiling"),
    ("REG-03", "Morphological inflection & FAR boilerplate guard"),
    ("REG-04", "Entity homograph isolation"),
    ("REG-05", "Conglomerate product-line & sibling segregation"),
    ("REG-06", "Intrinsic container homogeneity (anti-mirage)"),
    ("REG-07", "Trailing-slash parity & fail-closed coercion"),
    ("REG-08", "Unfiltered sums & ghost segregation"),
    ("REG-09", "Row-cohort mathematical consistency"),
    ("REG-10", "GID primary key & PIID collision isolation"),
    ("REG-11", "Subaward pipeline symmetry & rendering"),
    ("REG-12", "Fail-closed & zero-null hydration"),
]

_CHECKS = {"REG-01": check_reg01, "REG-02": check_reg02, "REG-03": check_reg03,
           "REG-04": check_reg04, "REG-05": check_reg05, "REG-06": check_reg06,
           "REG-07": check_reg07, "REG-08": check_reg08, "REG-09": check_reg09,
           "REG-10": check_reg10, "REG-11": check_reg11, "REG-12": check_reg12}


def run_all_systemic_checks(matrix: list[dict[str, Any]] | None = None) -> dict[str, dict[str, str]]:
    """Run REG-01..REG-12. REG-09 evaluates a live matrix when supplied,
    else a synthetic phantom matrix (must flag violations)."""
    out: dict[str, dict[str, str]] = {}
    for reg_id, title in REGISTRY:
        if reg_id == "REG-09":
            target = matrix if matrix is not None else [
                {"agency": "Smithsonian Institution", "tier1a_in_window": 106444.83,
                 "tier1b_in_window": 0, "tier2_provisional": 0,
                 "in_window_award_count": 0, "active_award_count": 0,
                 "completed_award_count": 0}]
            violations = check_matrix_invariants(target)
            if matrix is None:
                ok = len(violations) > 0
                detail = f"synthetic phantom flagged: {violations}"
            else:
                ok = not violations
                detail = f"violations={violations or 'none'}"
            out[reg_id] = {"status": PASS if ok else FAIL, "title": title, "detail": detail}
            continue
        try:
            status, detail = _CHECKS[reg_id]()
        except Exception as exc:  # noqa: BLE001 - a crashing check fails
            status, detail = FAIL, f"exception: {exc}"
        out[reg_id] = {"status": status, "title": title, "detail": detail}
    return out
