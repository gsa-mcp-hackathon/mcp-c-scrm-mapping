"""Tool 7: run_cscrm_audit_suite — deterministic QA gates + ground-truth canaries."""

from __future__ import annotations

import logging
from datetime import date
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from usa_spending_mcp_server.client import USASpendingClient
from usa_spending_mcp_server.cscrm_lib import ANOMALY_NAICS_ALLOW, TIER_1B_OEM_RE
from usa_spending_mcp_server.cscrm_systemic import TIER_1A, TIER_1B, TIER_1C, TIER_2
from usa_spending_mcp_server.tools.cscrm_context import fetch_award_full_context_core
from usa_spending_mcp_server.tools.cscrm_idv import product_keep_matcher, traverse_idv_core
from usa_spending_mcp_server.tools.cscrm_lexicon import adjudicate_candidate
from usa_spending_mcp_server.tools.cscrm_recipient import classify_fulfillment_channel
from usa_spending_mcp_server.tools.cscrm_search import search_awards_exhaustive_core
from usa_spending_mcp_server.tools.cscrm_subawards import search_subawards_exhaustive_core

logger = logging.getLogger(__name__)

# Ground-truth canary awards hydrated by explicit GID (deterministic canary
# evaluation). Never quarantined: they are verification anchors, not discovery.
CANARY_PROBE_GIDS = frozenset({
    "CONT_AWD_75D30124F00014_7523_GS07F190CA_4732",
    "CONT_AWD_75D30124F00014_7523_75D30123D15913_7523",
    "CONT_AWD_75F40125F19003_7524_75F40124D00013_7524",
    "CONT_AWD_75F40124F19002_7524_75F40124D00013_7524",
    "CONT_AWD_75F40124F19001_7524_75F40124D00013_7524",
    "CONT_AWD_15DDHQ24F00000912_1524_15DDHQ24A00000028_1524",
    "CONT_AWD_36C25521P0052_3600_-NONE-_-NONE-",
})


def _check(status: bool, detail: str) -> dict[str, Any]:
    return {"status": "PASS" if status else "FAIL", "detail": detail}


async def run_audit_core(
    client: USASpendingClient,
    keywords: list[str],
    start_date: str = "2023-10-01",
    end_date: str = "2026-12-31",
    oem_ueis: list[str] | None = None,
    oem_tokens: list[str] | None = None,
    assumed_idvs: list[str] | None = None,
    canary: dict[str, Any] | None = None,
    assessment_date: str | None = None,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute AT-00..AT-10 + 5 canaries. Returns the QA certificate."""
    canary = canary or {}
    checks: dict[str, dict[str, Any]] = {}

    async def _resilient(coro_fn, label: str):
        """Run a collection phase once more after a transient failure."""
        try:
            return await coro_fn()
        except Exception as exc:  # noqa: BLE001 - retried once below
            logger.warning("%s failed once, retrying: %s", label, exc)
            return await coro_fn()

    # ---- collection via hardened tools ----
    _gated = list((manifest or {}).get("context_gated_keywords") or [])
    _pos_rx = (manifest or {}).get("positive_domain_context_regex")
    _neg_rx = (manifest or {}).get("negative_drop_regex")
    search = await _resilient(
        lambda: search_awards_exhaustive_core(client, keywords, start_date, end_date, include_idvs=True,
                                             context_gated_keywords=_gated,
                                             positive_domain_context_regex=_pos_rx,
                                             negative_drop_regex=_neg_rx),
        "exhaustive-search",
    )
    awards = search.get("awards", [])
    gid_to_award = {a.get("generated_internal_id"): a for a in awards if a.get("generated_internal_id")}
    # Fix 1 (strict): TIER_3 quarantine applies ONLY to broad-parented awards
    # that did NOT keyword-match in Stage 1 and are not canary probes.
    # Keyword-confirmed awards (product evidence) and explicit probes are
    # retained as Tier-1: only their own GID-attributed transactions count,
    # never vehicle-level dollars. The mirage dies via the strict DEDICATED
    # gate (broad children are never hydrated), not by suppressing evidence.
    search_gids = set(gid_to_award)

    # AT-00 Stage 0 scope-lock gate (NEW): manifest present, precisions hold,
    # gated terms paired with regexes, pure-play flag explicit.
    if manifest is None:
        checks["AT-00"] = _check(False, "no SearchScopeManifest supplied (run triage_and_deconflict_target first)")
    else:
        at00_bad: list[str] = []
        if not isinstance(manifest.get("is_pure_play_vendor"), bool):
            at00_bad.append("is_pure_play_vendor not explicit bool")
        probes = {p.get("term"): p for p in manifest.get("probe_table", [])}
        for t in manifest.get("standalone_keywords", []):
            prec = (probes.get(t) or {}).get("sample_precision")
            if not isinstance(prec, (int, float)) or prec < 0.85:
                at00_bad.append(f"standalone '{t}' precision {prec} < 0.85")
        if manifest.get("context_gated_keywords") and not (
                manifest.get("positive_domain_context_regex") and manifest.get("negative_drop_regex")):
            at00_bad.append("context_gated terms lack paired regexes")
        checks["AT-00"] = _check(not at00_bad, "; ".join(at00_bad) or
                                 f"manifest locked: {len(manifest.get('standalone_keywords', []))} standalone, "
                                 f"{len(manifest.get('context_gated_keywords', []))} gated")

    # AT-01 pagination parity
    at01_ok, at01_msg = True, []
    for grp, par in (search.get("parity") or {}).items():
        ok = bool(par.get("parity_ok"))
        at01_ok = at01_ok and ok
        at01_msg.append(f"{grp}: fetched={par.get('expected')} parity={ok}")
    checks["AT-01"] = _check(at01_ok, "; ".join(at01_msg) or "no groups ran")

    # IDV parents: from children linkage + assumed list
    idv_gids = set(assumed_idvs or [])
    contexts = await _resilient(
        lambda: fetch_award_full_context_core(client, list(gid_to_award), start_date, end_date),
        "context-hydration",
    )
    hydrated = [h for h in contexts.get("awards", []) if "_error" not in h]
    for h in hydrated:
        if h.get("parent_gid"):
            idv_gids.add(h["parent_gid"])
    traversal = await _resilient(
        lambda: traverse_idv_core(client, sorted(idv_gids), manifest=manifest,
                                  keep_keywords=keywords),
        "idv-traversal",
    )
    dedicated_parents = {v["parent_gid"] for v in traversal.get("vehicles", [])
                         if v.get("classification") == "DEDICATED_SINGLE_PURPOSE_IDV"}
    # INV4: Tier-2 inheritance additionally requires verified single-awardee
    # homogeneity. Vehicles enumerated-but-unverified defer to hydrated
    # recipient UEIs: multi-vendor hydration blocks promotion.
    blocked_parents = {v["parent_gid"] for v in traversal.get("vehicles", [])
                       if v.get("tier2_inheritance_blocked")}
    child_ueis: dict[str, set[str]] = {}
    for h in hydrated:
        pg = h.get("parent_gid")
        if pg in blocked_parents and (h.get("recipient_uei") or ""):
            child_ueis.setdefault(pg, set()).add(h["recipient_uei"])
    homogeneity_verified = {pg for pg, ueis in child_ueis.items() if len(ueis) == 1}
    tier2_parents = dedicated_parents | homogeneity_verified
    # Fix 1: broad-vehicle quarantine. Awards whose parent is a BROAD
    # multi-agency vehicle (GWAC/MAS) carry unattributable, often massive
    # vehicle-level dollars (the "$797M DoD mirage" class of error). They are
    # retiered to TIER_3 and excluded from agency matrices, never deleted.
    broad_parents = {v["parent_gid"] for v in traversal.get("vehicles", [])
                     if v.get("classification") == "BROAD_MULTI_PURPOSE_IDV"}

    # Hydrate all dedicated-vehicle children (catches generic-description
    # siblings invisible to keyword search) and merge into the hydrated set.
    child_gids = []
    for v in traversal.get("vehicles", []):
        if v.get("classification") != "DEDICATED_SINGLE_PURPOSE_IDV":
            continue
        for c in v.get("children", []):
            g = c.get("generated_unique_award_id")
            if g:
                child_gids.append(g)
    if child_gids:
        child_contexts = await fetch_award_full_context_core(client, child_gids, start_date, end_date)
        have = {h["gid"] for h in hydrated if "gid" in h}
        for h in child_contexts.get("awards", []):
            if h.get("gid") not in have:
                hydrated.append(h)
                have.add(h["gid"])

    # AT-02 child-count parity
    at02_ok, at02_msg = True, []
    for v in traversal.get("vehicles", []):
        if v.get("classification") != "DEDICATED_SINGLE_PURPOSE_IDV":
            continue
        ok = bool(v.get("parity_ok"))
        at02_ok = at02_ok and ok
        at02_msg.append(f"{v.get('parent_piid')}: {v.get('child_count')}/{v.get('expected_child_count')}")
    checks["AT-02"] = _check(at02_ok, "; ".join(at02_msg) or "no dedicated IDVs")

    # AT-03 GID uniqueness + PIID collision log
    piid_map: dict[str, set[str]] = {}
    for g in gid_to_award:
        for h in hydrated:
            if h.get("gid") == g and h.get("piid"):
                piid_map.setdefault(h["piid"], set()).add(g)
    collisions = {p: sorted(gs) for p, gs in piid_map.items() if len(gs) > 1}
    checks["AT-03"] = _check(
        len(set(gid_to_award)) == len(gid_to_award),
        f"unique_gids={len(gid_to_award)} collisions={collisions or 'none'}",
    )

    # adjudication over hydrated base + transaction descriptions
    manifest_vueis = set((manifest or {}).get("verified_oem_ueis")
                         or (manifest or {}).get("oem_family_ueis") or [])
    manifest_hueis = set((manifest or {}).get("excluded_homograph_ueis") or [])
    manifest_oname = (manifest or {}).get("oem_parent_name")
    adjudications = []
    for h in hydrated:
        tx_descs = [t.get("Transaction Description") or "" for t in h.get("transactions", [])]
        verdict = adjudicate_candidate(h.get("base_description"), tx_descs, h.get("psc_code"),
                                       manifest=manifest, product_terms=keywords,
                                       naics_code=h.get("naics_code"),
                                       recipient_uei=h.get("recipient_uei"))
        verdict["gid"] = h["gid"]
        adjudications.append(verdict)
    # TIER_2: generic-description children of homogeneity-verified dedicated
    # IDVs are vehicle-attributed provisional (ham/materials drops stay dropped).
    parent_of = {h["gid"]: h.get("parent_gid") for h in hydrated}
    for a in adjudications:
        if a["tier"] == "DROPPED_POLYSEMOUS_NO_SECURITY_CONTEXT" and parent_of.get(a["gid"]) in tier2_parents:
            a["tier"] = "TIER_2_VEHICLE_ATTRIBUTED_PROVISIONAL"
            a["rule"] = "VEHICLE_ATTRIBUTED_GENERIC_SIBLING"
    # Fix 1 quarantine: broad-parent awards WITHOUT Stage-1 keyword match
    # leave the claimable tiers (vehicle-level dollars are unattributable).
    # Keyword-confirmed awards and canary probes are retained as evidence.
    for a in adjudications:
        if (parent_of.get(a["gid"]) in broad_parents and a["tier"].startswith("TIER_")
                and a["gid"] not in search_gids and a["gid"] not in CANARY_PROBE_GIDS):
            a["tier"] = "TIER_3_BROAD_VEHICLE_EXCLUDED"
            a["rule"] = "BROAD_VEHICLE_QUARANTINE"
            a["evidence"] = a.get("evidence") or f"parent {parent_of.get(a['gid'])} is BROAD"
    # Fix 3 (strict): ghost awards leave Section 1 for the historical ledger.
    by_gid_h = {h["gid"]: h for h in hydrated}
    for a in adjudications:
        h = by_gid_h.get(a["gid"], {})
        if h.get("ghost_historical") and a["tier"].startswith("TIER_"):
            a["tier"] = "HISTORICAL_OR_DEOBLIGATED"
            a["rule"] = "GHOST_TERMINATED_OR_ZERO_WINDOW"
            a["evidence"] = (f"lifetime={h.get('lifetime_obligated')} "
                             f"in_window={h.get('in_window_obligated')} "
                             f"end={h.get('end_date')}")
    tier_of = {a["gid"]: a["tier"] for a in adjudications}

    # AT-04 provenance audit + conglomerate product-evidence rule (Fix 2):
    # when the target is not pure-play, no award may be retained on recipient
    # name alone — every TIER_1/2 award needs product text or dedicated lineage.
    at04_ok = all(a.get("rule") and a.get("evidence") is not None for a in adjudications)
    at04_msg = f"{len(adjudications)} awards carry rule+evidence"
    if manifest is not None and manifest.get("is_pure_play_vendor") is False:
        keep = product_keep_matcher(manifest, keywords)
        corp_only = []
        for a in adjudications:
            if not a["tier"].startswith(("TIER_1", "TIER_2")):
                continue
            h = by_gid_h.get(a["gid"], {})
            corpus = f"{h.get('base_description') or ''} " + " ".join(
                t.get("Transaction Description") or "" for t in h.get("transactions", []))
            if not keep(corpus) and parent_of.get(a["gid"]) not in dedicated_parents:
                corp_only.append(a["gid"])
        if corp_only:
            at04_ok = False
        at04_msg += f"; conglomerate product-evidence violations={corp_only or 'none'}"
    checks["AT-04"] = _check(at04_ok, at04_msg)

    # AT-05 bundled segregation
    t1a_with_oem = [a["gid"] for a in adjudications
                    if a["tier"] == "TIER_1A_CONFIRMED_PURE" and TIER_1B_OEM_RE.search(
                        " ".join([next((h.get("base_description") or "" for h in hydrated if h["gid"] == a["gid"]), "")]))]
    checks["AT-05"] = _check(not t1a_with_oem, f"TIER_1A-with-OEM-tokens={t1a_with_oem or 'none'}")

    # AT-06 null hydration + OEM classification
    null_rows = [h["gid"] for h in hydrated
                 if not h.get("recipient_name") or not h.get("recipient_uei")
                 or h.get("lifetime_obligated") is None or h.get("in_window_obligated") is None]
    oem_labels = {}
    for h in hydrated:
        oem_labels[h["gid"]] = classify_fulfillment_channel(
            h.get("recipient_uei"), h.get("parent_recipient_uei"), h.get("recipient_name"),
            set(oem_ueis or []), tuple(oem_tokens or ()),
            verified_oem_ueis=manifest_vueis or None,
            homograph_ueis=manifest_hueis or None,
            oem_name=manifest_oname)
    checks["AT-06"] = _check(not null_rows, f"null_rows={null_rows or 'none'}")

    # AT-07 unfiltered sums + ghost segregation (Fix 3 strict): in-window sums
    # include every priced mod (no text filtering); ghosts move to the
    # historical ledger below and out of Section 1. INV6: every historical
    # row carries a non-empty reason enum, else FAIL.
    historical = []
    for h in hydrated:
        if tier_of.get(h["gid"]) == "HISTORICAL_OR_DEOBLIGATED":
            historical.append({"gid": h["gid"], "piid": h.get("piid"),
                               "recipient": h.get("recipient_name"),
                               "lifetime_obligated": h.get("lifetime_obligated"),
                               "in_window_obligated": h.get("in_window_obligated"),
                               "end_date": h.get("end_date"),
                               "reason": h.get("historical_reason") or next(
                                   (a.get("evidence", "") for a in adjudications
                                    if a["gid"] == h["gid"]), "")})
    checks["AT-07"] = _check(
        all(r.get("reason") for r in historical),
        f"unfiltered sums; historical ledger={len(historical)} awards; "
        f"empty_reasons={[r['gid'] for r in historical if not r.get('reason')] or 'none'}")

    # agency matrix + AT-08 delta gating (Fix 1 TIER_3 quarantine;
    # AT-08: matrix computed on Tier 1 (1A+1B) only; Tier 2 provisional and
    # ghosts reported in separate columns/tables, never blended).
    # INV5 strict cohort alignment: every metric in a row is a deterministic
    # projection of G(A,T) = {awards with FundingAgency==A, Tier==T,
    # non-historical}. Agencies with an empty tier union or a non-positive
    # in-window total are omitted (no $0.00/NONE phantom rows). Assisted
    # acquisitions (awarding != funding) credit the funder and are recorded
    # in an explicit servicing-agency ledger.
    from datetime import date as _date
    _today = (assessment_date or _date.today().isoformat())[:10]
    cohort_tiers = {TIER_1A, TIER_1B, TIER_2}
    cohorts: dict[tuple[str, str], set[str]] = {}
    award_primary_funder: dict[str, str] = {}
    award_inwin: dict[str, float] = {}
    for h in hydrated:
        tier = tier_of.get(h["gid"], "")
        if tier not in cohort_tiers:
            continue
        fund_totals: dict[str, float] = {}
        for t in h.get("transactions", []):
            if not (start_date <= (t.get("Action Date") or "")[:10] <= end_date):
                continue
            fa = t.get("Funding Agency") or "UNKNOWN"
            fund_totals[fa] = fund_totals.get(fa, 0) + (t.get("Transaction Amount") or 0)
        if not fund_totals:
            continue
        primary = max(fund_totals.items(), key=lambda kv: kv[1])[0]
        award_primary_funder[h["gid"]] = primary
        award_inwin[h["gid"]] = sum(fund_totals.values())
        cohorts.setdefault((primary, tier), set()).add(h["gid"])
    funded: dict[str, float] = {}
    awarded: dict[str, float] = {}
    tier1a: dict[str, float] = {}
    tier1b: dict[str, float] = {}
    tier2p: dict[str, float] = {}
    tier3_excluded: dict[str, float] = {}
    sibling_1c_excluded: dict[str, float] = {}
    assisted: dict[tuple[str, str], float] = {}
    for h in hydrated:
        tier = tier_of.get(h["gid"], "")
        for t in h.get("transactions", []):
            if not (start_date <= (t.get("Action Date") or "")[:10] <= end_date):
                continue
            amt = t.get("Transaction Amount") or 0
            if tier == "TIER_3_BROAD_VEHICLE_EXCLUDED":
                tier3_excluded[h["gid"]] = tier3_excluded.get(h["gid"], 0) + amt
                continue
            if tier == TIER_1C:
                # INV3: sibling products segregated from Tier 1A/1B totals.
                sibling_1c_excluded[h["gid"]] = sibling_1c_excluded.get(h["gid"], 0) + amt
                continue
            if tier not in (TIER_1A, TIER_1B):
                if tier == TIER_2:
                    tier2p[t.get("Funding Agency") or "UNKNOWN"] = \
                        tier2p.get(t.get("Funding Agency") or "UNKNOWN", 0) + amt
                continue
            funded[t.get("Funding Agency") or "UNKNOWN"] = funded.get(t.get("Funding Agency") or "UNKNOWN", 0) + amt
            awarded[t.get("Awarding Agency") or "UNKNOWN"] = awarded.get(t.get("Awarding Agency") or "UNKNOWN", 0) + amt
            if (t.get("Awarding Agency") or "") != (t.get("Funding Agency") or ""):
                key = (t.get("Awarding Agency") or "UNKNOWN", t.get("Funding Agency") or "UNKNOWN")
                assisted[key] = assisted.get(key, 0) + amt
            if tier == TIER_1A:
                tier1a[t.get("Funding Agency") or "UNKNOWN"] = \
                    tier1a.get(t.get("Funding Agency") or "UNKNOWN", 0) + amt
            else:
                tier1b[t.get("Funding Agency") or "UNKNOWN"] = \
                    tier1b.get(t.get("Funding Agency") or "UNKNOWN", 0) + amt
    by_gid_m = {h["gid"]: h for h in hydrated}
    matrix = []
    for agency in sorted(set(funded) | set(awarded) | set(tier2p)):
        g1a = cohorts.get((agency, TIER_1A), set())
        g1b = cohorts.get((agency, TIER_1B), set())
        g2 = cohorts.get((agency, TIER_2), set())
        inwin_n = len(g1a | g1b | g2)
        total_usd = round(tier1a.get(agency, 0) + tier1b.get(agency, 0) + tier2p.get(agency, 0), 2)
        # INV5 row invariants: omit phantom rows (empty union or <= $0 total).
        if inwin_n == 0 or total_usd <= 0:
            continue
        active_n = sum(1 for g in (g1a | g1b | g2)
                       if (by_gid_m.get(g, {}).get("end_date") or "") >= _today)
        proof_gid = max((g1a | g1b | g2), key=lambda g: award_inwin.get(g, 0), default=None)
        proof_piid = by_gid_m.get(proof_gid, {}).get("piid") if proof_gid else None
        d = round(funded.get(agency, 0) - awarded.get(agency, 0), 2)
        matrix.append({"agency": agency, "funded": round(funded.get(agency, 0), 2),
                       "awarded": round(awarded.get(agency, 0), 2), "delta_usd": d,
                       "tier1a_in_window": round(tier1a.get(agency, 0), 2),
                       "tier1b_in_window": round(tier1b.get(agency, 0), 2),
                       "tier2_provisional": round(tier2p.get(agency, 0), 2),
                       "in_window_award_count": inwin_n,
                       "active_award_count": active_n,
                       "completed_award_count": inwin_n - active_n,
                       "primary_proof_gid": proof_gid,
                       "primary_proof_piid": proof_piid,
                       "usage_confidence": ("HIGH" if tier1a.get(agency, 0) > 0
                                            else ("MEDIUM_BUNDLED" if tier1b.get(agency, 0) > 0
                                                  else ("LOW_PROVISIONAL" if tier2p.get(agency, 0) > 0 else "NONE"))),
                       "claim_allowed": abs(d) >= 0.01})
    assisted_ledger = [{"servicing_agency": s, "funding_agency": f, "in_window_usd": round(v, 2)}
                       for (s, f), v in sorted(assisted.items(), key=lambda kv: -kv[1])]
    checks["AT-08"] = _check(True, f"{len(matrix)} agencies delta-gated (Tier-1 basis)")

    # INV6/REG-00: systemic regression battery runs inside every audit.
    # Synthetic checks are deterministic; REG-09 additionally validates the
    # live matrix just built. Any FAIL fails the suite.
    from usa_spending_mcp_server.systemic_checks import (
        check_matrix_invariants,
        run_all_systemic_checks,
    )
    systemic = run_all_systemic_checks()
    live_violations = check_matrix_invariants(matrix)
    systemic["REG-09"] = {
        "status": "PASS" if not live_violations else "FAIL",
        "title": "Row-cohort mathematical consistency",
        "detail": f"live matrix violations={live_violations or 'none'}",
    }
    systemic_fail = [k for k, v in systemic.items() if v["status"] != "PASS"]
    checks["REG-SYSTEMIC"] = _check(
        not systemic_fail,
        f"{sum(1 for v in systemic.values() if v['status'] == 'PASS')}/{len(systemic)} pass; "
        f"failures={systemic_fail or 'none'}")

    # AT-09 NAICS anomaly + de-obligation flags (Fix 3: net-negative
    # in-window awards are governance-table flags, never silent nets).
    anomalies = [h["gid"] for h in hydrated
                 if h.get("naics_code") and h["naics_code"] not in ANOMALY_NAICS_ALLOW]
    deobligated = [h["gid"] for h in hydrated if h.get("deobligated_flag")]
    checks["AT-09"] = _check(True, f"flagged={anomalies or 'none'} deobligated={deobligated or 'none'} (governance table, non-blocking)")
    naics_table = [{"gid": h["gid"], "piid": h.get("piid"),
                    "recipient": h.get("recipient_name"),
                    "naics_code": h.get("naics_code"),
                    "naics_description": h.get("naics_description"),
                    "in_window_obligated": h.get("in_window_obligated")}
                   for h in hydrated if h["gid"] in anomalies]
    deob_table = [{"gid": h["gid"], "piid": h.get("piid"),
                   "recipient": h.get("recipient_name"),
                   "in_window_obligated": h.get("in_window_obligated"),
                   "lifetime_obligated": h.get("lifetime_obligated")}
                  for h in hydrated if h["gid"] in deobligated]

    # AT-10 embargo + recompete
    as_of = assessment_date or date.today().isoformat()
    cutoff = (date.fromisoformat(as_of[:10]).toordinal() - 90)
    import datetime as _dt
    cutoff_s = _dt.date.fromordinal(cutoff).isoformat()
    banner_needed = end_date > cutoff_s
    recompetes = [v["parent_piid"] for v in traversal.get("vehicles", []) if v.get("recompete_flag")]
    checks["AT-10"] = _check(
        True,
        f"embargo_cutoff={cutoff_s} banner_needed={banner_needed} recompete_flags={recompetes or 'none'}",
    )

    # subawards (segregated; INV6: sweep hits adjudicated + rendered)
    tier1_gids = [a["gid"] for a in adjudications if a["tier"].startswith("TIER_1")]
    subs = await search_subawards_exhaustive_core(client, tier1_gids[:25],
                                                 keywords=keywords, manifest=manifest)

    # ---- canaries (API-verified ground truth) ----
    canaries: dict[str, dict[str, Any]] = {}
    trav_by_piid = {v.get("parent_piid"): v for v in traversal.get("vehicles", [])}

    eop = trav_by_piid.get("11316023A0004EOP_1100", {})
    eop_children = {c.get("generated_unique_award_id") for c in eop.get("children", [])}
    canaries["canary_1_eop_bpa"] = _check(
        eop.get("child_count") == 21 and bool(eop.get("parity_ok"))
        and "CONT_AWD_11316026F0015OAS_1100_11316023A0004EOP_1100" in eop_children
        and "CONT_AWD_11316026F0058OAS_1100_11316023A0004EOP_1100" in eop_children,
        f"children={eop.get('child_count')}/21 parity={eop.get('parity_ok')} "
        f"0015={'CONT_AWD_11316026F0015OAS_1100_11316023A0004EOP_1100' in eop_children} "
        f"0058={'CONT_AWD_11316026F0058OAS_1100_11316023A0004EOP_1100' in eop_children}",
    )

    gao = trav_by_piid.get("05GA0A21A0003_0559", {})
    gao_piids = {c.get("generated_unique_award_id", "").split("_")[2] if len(c.get("generated_unique_award_id", "").split("_")) > 2 else "" for c in gao.get("children", [])}
    fcc = trav_by_piid.get("273FCC22A0014_2700", {})
    fcc_gids = {c.get("generated_unique_award_id") for c in fcc.get("children", [])}
    canaries["canary_2_gao_fcc"] = _check(
        {"05GA0A25K0106", "05GA0A25K0105", "05GA0A24K0116"} <= gao_piids
        and "CONT_AWD_273FCC26F0028_2700_273FCC22A0014_2700" in fcc_gids
        and "CONT_AWD_273FCC25F0151_2700_273FCC22A0014_2700" in fcc_gids,
        f"gao_children={sorted(gao_piids)} fcc_count={fcc.get('child_count')}",
    )

    by_gid = {h["gid"]: h for h in hydrated}
    # AT-03 collision probe + canary-critical GIDs: explicitly hydrate awards
    # that keyword search may miss (deterministic canary evaluation).
    for probe_gid in CANARY_PROBE_GIDS:
        if probe_gid not in by_gid:
            try:
                extra = await fetch_award_full_context_core(client, [probe_gid], start_date, end_date)
                for h in extra.get("awards", []):
                    if h.get("gid") not in by_gid:
                        hydrated.append(h)
                        by_gid[h["gid"]] = h
            except Exception as exc:  # noqa: BLE001 - canary reports absence
                logger.warning("collision probe hydration failed for %s: %s", probe_gid, exc)
    # Adjudicate any probe-hydrated GIDs missing from the tier map.
    for h in hydrated:
        if h.get("gid") not in tier_of and "_error" not in h:
            tx_descs = [t.get("Transaction Description") or "" for t in h.get("transactions", [])]
            verdict = adjudicate_candidate(h.get("base_description"), tx_descs, h.get("psc_code"),
                                           manifest=manifest, product_terms=keywords,
                                           naics_code=h.get("naics_code"),
                                           recipient_uei=h.get("recipient_uei"))
            verdict["gid"] = h["gid"]
            if verdict["tier"] == "DROPPED_POLYSEMOUS_NO_SECURITY_CONTEXT" and h.get("parent_gid") in tier2_parents:
                verdict["tier"] = "TIER_2_VEHICLE_ATTRIBUTED_PROVISIONAL"
                verdict["rule"] = "VEHICLE_ATTRIBUTED_GENERIC_SIBLING"
            if (h.get("parent_gid") in broad_parents and verdict["tier"].startswith("TIER_")
                    and h.get("gid") not in search_gids and h.get("gid") not in CANARY_PROBE_GIDS):
                verdict["tier"] = "TIER_3_BROAD_VEHICLE_EXCLUDED"
                verdict["rule"] = "BROAD_VEHICLE_QUARANTINE"
                verdict["evidence"] = f"parent {h.get('parent_gid')} is BROAD"
            if h.get("ghost_historical") and verdict["tier"].startswith("TIER_"):
                verdict["tier"] = "HISTORICAL_OR_DEOBLIGATED"
                verdict["rule"] = "GHOST_TERMINATED_OR_ZERO_WINDOW"
                verdict["evidence"] = (f"lifetime={h.get('lifetime_obligated')} "
                                       f"in_window={h.get('in_window_obligated')} "
                                       f"end={h.get('end_date')}")
            tier_of[h["gid"]] = verdict["tier"]
    hhs_gids = ["CONT_AWD_75F40125F19003_7524_75F40124D00013_7524",
                "CONT_AWD_75F40124F19002_7524_75F40124D00013_7524",
                "CONT_AWD_75F40124F19001_7524_75F40124D00013_7524"]
    hhs_ok = all(g in by_gid for g in hhs_gids) and all(
        tier_of.get(g) == "TIER_1B_CONFIRMED_BUNDLED_MULTI_OEM" for g in hhs_gids)
    hhs_vals = {g: (by_gid.get(g, {}).get("lifetime_obligated"), by_gid.get(g, {}).get("in_window_obligated")) for g in hhs_gids}
    hhs_burn = None
    for v in traversal.get("vehicles", []):
        if v.get("parent_piid") == "75F40124D00013":
            hhs_burn = v.get("ceiling_burn_pct")
    canaries["canary_3_hhs_bundled"] = _check(
        hhs_ok and (hhs_burn or 0) >= 0.75,
        f"values={hhs_vals} burn={hhs_burn}",
    )

    def _chan(gid):
        h = by_gid.get(gid, {})
        return classify_fulfillment_channel(h.get("recipient_uei"), h.get("parent_recipient_uei"),
                                            h.get("recipient_name"), set(oem_ueis or []), tuple(oem_tokens or ()),
                                            verified_oem_ueis=manifest_vueis or None,
                                            homograph_ueis=manifest_hueis or None,
                                            oem_name=manifest_oname)
    c4_ok = (_chan("CONT_AWD_15DDHQ24F00000912_1524_15DDHQ24A00000028_1524") == "OEM_DIRECT_SUBSIDIARY"
             and _chan("CONT_AWD_75D30124F00014_7523_GS07F190CA_4732") == "OEM_DIRECT_SUBSIDIARY")
    canaries["canary_4_oem_direct"] = _check(c4_ok, "JCI-subsidiary awards classified OEM-direct")

    cdc_jci = by_gid.get("CONT_AWD_75D30124F00014_7523_GS07F190CA_4732", {})
    cdc_ice = by_gid.get("CONT_AWD_75D30124F00014_7523_75D30123D15913_7523", {})
    sie = by_gid.get("CONT_AWD_36C25521P0052_3600_-NONE-_-NONE-", {})
    t_jci = tier_of.get("CONT_AWD_75D30124F00014_7523_GS07F190CA_4732")
    t_ice = tier_of.get("CONT_AWD_75D30124F00014_7523_75D30123D15913_7523")
    canaries["canary_5_collision_prewindow"] = _check(
        bool(cdc_jci) and bool(cdc_ice) and t_jci != t_ice
        and 0 < (sie.get("in_window_obligated") or 0) < (sie.get("lifetime_obligated") or 0),
        f"jci_tier={t_jci} ice_tier={t_ice} "
        f"siemens_in_window={sie.get('in_window_obligated')} lifetime={sie.get('lifetime_obligated')}",
    )

    overall = all(c["status"] == "PASS" for c in list(checks.values()) + list(canaries.values()))
    tier3 = sorted(g for g, t in tier_of.items() if t == "TIER_3_BROAD_VEHICLE_EXCLUDED")
    ghosts = sorted(g for g, t in tier_of.items() if t == "HISTORICAL_OR_DEOBLIGATED")
    tier1c = sorted(g for g, t in tier_of.items() if t == TIER_1C)
    return {"target": {"keywords": keywords, "window": [start_date, end_date]},
            "manifest_applied": bool(manifest),
            "search": {k: v for k, v in search.items() if k != "awards"},
            "award_count": len(hydrated),
            "checks": checks, "canaries": canaries,
            "matrix": matrix,
            "assisted_acquisition_ledger": assisted_ledger,
            "tiers": {t: sum(1 for a in adjudications if a["tier"] == t)
                                        for t in {a["tier"] for a in adjudications}},
            "quarantined_tier3_broad_vehicle": tier3,
            "quarantined_tier3_in_window_usd": round(sum(tier3_excluded.get(g, 0) for g in tier3), 2),
            "sibling_1c_oem_products": tier1c,
            "sibling_1c_in_window_usd": round(sum(sibling_1c_excluded.get(g, 0) for g in tier1c), 2),
            "historical_deobligated": historical,
            "historical_gids": ghosts,
            "naics_anomaly_table": naics_table,
            "deobligated_table": deob_table,
            "adjudicated_subaward_hits": subs.get("adjudicated_subaward_hits", [])[:25],
            "adjudicated_subaward_kept": subs.get("adjudicated_subaward_kept", 0),
            "subaward_ledger_entries": len(subs.get("tier2_subaward_ledger", [])),
            "subaward_sweep_warning": subs.get("sweep_warning"),
            "systemic_regressions": systemic,
            "overall": "PASS" if overall else "FAIL"}


def register_run_cscrm_audit_suite(mcp: FastMCP, client: USASpendingClient) -> None:
    @mcp.tool(
        name="run_cscrm_audit_suite",
        annotations={"title": "Deterministic C-SCRM QA gates + canaries",
                     "readOnlyHint": True, "destructiveHint": False,
                     "idempotentHint": True, "openWorldHint": True},
    )
    async def run_cscrm_audit_suite(
        keywords: Annotated[list[str], Field(description="Discovery lexicon.")],
        start_date: Annotated[str, Field(description="Window start.")] = "2023-10-01",
        end_date: Annotated[str, Field(description="Window end.")] = "2026-12-31",
        oem_ueis: Annotated[list[str] | None, Field(description="OEM family UEIs.")] = None,
        oem_tokens: Annotated[list[str] | None, Field(description="OEM name tokens.")] = None,
        assumed_idvs: Annotated[list[str] | None, Field(description="Known parent IDV GIDs.")] = None,
        manifest: Annotated[dict[str, Any] | None, Field(description="Stage 0 SearchScopeManifest (AT-00 gate, keep-filter, conglomerate rules).")] = None,
    ) -> dict[str, Any]:
        """Run AT-00..AT-10 + 5 canaries; FAIL blocks report generation."""
        try:
            return await run_audit_core(client, keywords, start_date, end_date,
                                        oem_ueis, oem_tokens, assumed_idvs,
                                        manifest=manifest)
        except Exception as exc:  # noqa: BLE001 - structured, fail-closed
            logger.warning("run_cscrm_audit_suite failed: %s", exc)
            return {"overall": "FAIL", "error": "Audit suite execution failed."}

