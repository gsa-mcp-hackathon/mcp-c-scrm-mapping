"""Audit gates AT-00..AT-10 + five live canaries (deterministic QA).

Gate semantics (ported from the verified legacy suite, adapted to the
Stage 0-6 structures). Canaries evaluate live ground truth from
canaries.json (configurable per target). FAIL anywhere blocks PASS overall.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from usaspending_cscrm_mcp.certification import GateResult
from usaspending_cscrm_mcp.models import AwardRecord
from usaspending_cscrm_mcp.pipeline.stage1_search import Stage1Result
from usaspending_cscrm_mcp.pipeline.stage3_idv import VehicleResult
from usaspending_cscrm_mcp.pipeline.stage4_adjudicate import Adjudication
from usaspending_cscrm_mcp.pipeline.stage6_matrix import Stage6Result

STANDALONE_PRECISION_FLOOR = 0.85


def _gate(gate_id: str, ok: bool, detail: str) -> GateResult:
    return GateResult(gate_id=gate_id, status="PASS" if ok else "FAIL", detail=detail)


def default_canaries_config() -> dict[str, Any]:
    return json.loads((Path(__file__).parent / "canaries.json").read_text())


def at_00_scope_lock(manifest: Any) -> GateResult:
    if manifest is None:
        return _gate("AT-00", False, "no manifest supplied (run target_triage first)")
    bad: list[str] = []
    if manifest.target_archetype == "ARCHETYPE_2_CONGLOMERATE" and not manifest.is_conglomerate:
        bad.append("archetype/conglomerate flag incoherent")
    siblings = list(getattr(manifest, "sibling_terms", []) or [])
    if manifest.target_archetype == "ARCHETYPE_1_PURE_PLAY_VENDOR" and manifest.is_conglomerate:
        if siblings:
            pass  # handled contradiction: sibling segregation active, see detail
        else:
            bad.append("declared ARCHETYPE_1 with conglomerate signals but no "
                       "sibling_terms captured (set ARCHETYPE_2 or add sibling terms)")
    scores = {s.term: s for s in manifest.term_scores}
    for t in manifest.standalone_terms:
        score = scores.get(t)
        prec = score.precision if score is not None else None
        if not isinstance(prec, (int, float)) or prec < STANDALONE_PRECISION_FLOOR:
            bad.append(f"standalone '{t}' precision {prec} < 0.85")
    if manifest.context_gated_terms and not manifest.positive_regex:
        bad.append("context_gated terms lack positive_domain regex")
    return _gate("AT-00", not bad, "; ".join(bad) or
                 f"manifest locked: {len(manifest.standalone_terms)} standalone, "
                 f"{len(manifest.context_gated_terms)} gated"
                 + (f", effective {manifest.effective_archetype}"
                    if getattr(manifest, "effective_archetype", "") and
                    getattr(manifest, "effective_archetype", "") != manifest.target_archetype else "")
                 + (f", siblings={len(siblings)}" if siblings else ""))


def at_01_pagination_parity(stage1: Stage1Result) -> GateResult:
    msgs, ok = [], True
    for o in stage1.observations:
        good = o.complete and o.reported_count >= 0 and o.reported_count == o.fetched_count
        ok = ok and good
        msgs.append(f"{o.query_id}: reported={o.reported_count} fetched={o.fetched_count} "
                    f"complete={o.complete}")
    total_fetched = sum(max(o.fetched_count, 0) for o in stage1.observations)
    if total_fetched > 0 and not stage1.hits:
        ok = False
        msgs.append(f"vacuous run: {total_fetched} rows fetched but zero candidates "
                    f"retained (gated_filtered_out={stage1.gated_filtered_out})")
    return _gate("AT-01", ok, "; ".join(msgs) or "no populations ran")


def at_02_idv_parity(vehicles: list[VehicleResult]) -> GateResult:
    msgs, ok = [], True
    for v in vehicles:
        if v.classification != "DEDICATED_SINGLE_PURPOSE_IDV":
            continue
        global_n = getattr(v, "global_child_count", None)
        if global_n is None:
            ok = False
            msgs.append(f"{v.parent_piid}: local={v.child_count}/global=UNKNOWN "
                        f"(no global count — cannot verify dedication)")
            continue
        good = bool(v.parity_ok) and v.child_count == global_n
        ok = ok and good
        msgs.append(f"{v.parent_piid}: local={v.child_count}/global={global_n}")
    return _gate("AT-02", ok, "; ".join(msgs) or "no dedicated IDVs")


def at_03_identity(records: list[AwardRecord]) -> GateResult:
    gids = [r.gid for r in records]
    piids: dict[str, set[str]] = {}
    for r in records:
        if r.piid:
            piids.setdefault(r.piid, set()).add(r.gid)
    collisions = {p: sorted(gs) for p, gs in piids.items() if len(gs) > 1}
    return _gate("AT-03", len(set(gids)) == len(gids),
                 f"unique_gids={len(gids)} collisions={collisions or 'none'}")


def at_04_provenance(adjs: dict[str, Adjudication], records: list[AwardRecord],
                     is_conglomerate: bool, allow_name_only: bool) -> GateResult:
    ok = all(a.rule and a.snippet is not None for a in adjs.values())
    msg = f"{len(adjs)} adjudications carry rule+snippet"
    by_gid = {r.gid: r for r in records}
    corp_only: list[str] = []
    if is_conglomerate and not allow_name_only:
        for gid, a in adjs.items():
            if not a.tier.startswith(("Tier-1", "Tier-2")):
                continue
            rec = by_gid.get(gid)
            product_text = bool(rec and (rec.base_description or rec.transaction_descriptions))
            if a.basis != "product_associated" and not a.dedicated_lineage and not product_text:
                corp_only.append(gid)
        if corp_only:
            ok = False
        msg += f"; conglomerate name-only violations={corp_only or 'none'}"
    return _gate("AT-04", ok, msg)


def at_05_bundled_segregation(adjs: dict[str, Adjudication],
                              sibling_terms: list[str] | None = None) -> GateResult:
    import re as _re
    bad = [g for g, a in adjs.items()
           if a.tier == "Tier-1A" and getattr(a, "competing", [])]
    sib_res = [_re.compile(r"\b" + _re.escape(t.strip()) + r"\b", _re.I)
               for t in (sibling_terms or []) if t and t.strip()]
    sib_bad = [g for g, a in adjs.items()
               if a.tier == "Tier-1A" and a.snippet
               and any(rx.search(a.snippet) for rx in sib_res)]
    bad = sorted(set(bad) | set(sib_bad))
    return _gate("AT-05", not bad, f"Tier-1A-with-competitors={bad or 'none'}")


def _field(obj: Any, key: str, default: Any = "") -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def at_06_hydration_completeness(records: list[AwardRecord],
                                 subawards: list[Any] | None = None,
                                 matrix: Any | None = None) -> GateResult:
    null_rows = [r.gid for r in records
                 if not r.recipient_name or not r.recipient_uei
                 or r.lifetime_obligated is None
                 or r.award_lifecycle_status == "UNKNOWN"
                 or not r.hydration_complete]
    sub_blank = [_field(s, "subaward_number", "?") or "?" for s in (subawards or [])
                 if not _field(s, "subawardee_name")
                 or not _field(s, "awarding_agency")
                 or not _field(s, "description")]
    mat_blank: list[str] = []
    for row in (_field(matrix, "matrix", None) or []):
        agency, display = _field(row, "agency"), _field(row, "display")
        proofs = _field(row, "proof_gids", ["x"])
        if not agency or not (agency.strip() if isinstance(agency, str) else True) \
                or not display or not proofs:
            mat_blank.append(f"{agency or '?'}:{display or '?'}")
    bad = [f"prime:{g}" for g in null_rows] + [f"sub:{s}" for s in sub_blank] + \
        [f"matrix:{m}" for m in mat_blank]
    return _gate("AT-06", not bad, f"null_or_incomplete={bad or 'none'}")


def at_07_ghost_accounting(records: list[AwardRecord]) -> GateResult:
    bad = [r.gid for r in records if r.ghost_historical and not r.historical_reason]
    ghosts = [r.gid for r in records if r.ghost_historical]
    return _gate("AT-07", not bad,
                 f"ghosts={len(ghosts)} empty_reasons={bad or 'none'}")


def at_08_cohort_invariants(matrix: Stage6Result) -> GateResult:
    bad: list[str] = []
    for row in matrix.matrix:
        if not row.proof_gids:
            bad.append(f"{row.agency}/{row.display}: no proof")
        if row.award_count <= 0:
            bad.append(f"{row.agency}/{row.display}: empty cohort")
        if row.window_net != row.window_positive + row.window_negative:
            bad.append(f"{row.agency}/{row.display}: Decimal imbalance")
    return _gate("AT-08", not bad, "; ".join(bad) or
                 f"{len(matrix.matrix)} cohort rows invariant-clean")


def at_09_taxonomy_watch(records: list[AwardRecord]) -> GateResult:
    anomalies = [r.gid for r in records
                 if r.naics_code and r.naics_code[:3] in ("236", "237", "238")]
    deob = [r.gid for r in records if r.window.window_net_obligations < 0]
    return _gate("AT-09", True,
                 f"construction-taxonomy={anomalies or 'none'} "
                 f"net-negative={deob or 'none'} (governance table, non-blocking)")


def at_10_embargo_watch(vehicles: list[VehicleResult], window_end: str,
                        today: str | None = None) -> GateResult:
    as_of = today or date.today().isoformat()
    cutoff_ord = date.fromisoformat(as_of[:10]).toordinal() - 90
    cutoff = date.fromordinal(cutoff_ord).isoformat()
    banner = window_end > cutoff
    watch: list[str] = []
    expired: list[str] = []
    for v in vehicles:
        if v.days_remaining is None:
            continue
        if getattr(v, "expired_parent", False):
            expired.append(v.parent_piid)
        elif v.days_remaining < 180:
            watch.append(v.parent_piid)
    return _gate("AT-10", True,
                 f"embargo_cutoff={cutoff} banner_needed={banner} "
                 f"recompete_watch={watch or 'none'} expired={expired or 'none'}")


def competitor_regex_from_terms(terms: list[str] | None) -> str | None:
    """Build a whole-word competitor pattern from caller-supplied terms.

    Target config only — never vendor vocabulary. Returns None when empty.
    """
    clean = [t.strip() for t in (terms or []) if t and t.strip()]
    if not clean:
        return None
    import re as _re
    return r"\b(" + "|".join(_re.escape(t) for t in clean) + r")\b"


def _all_enumerated_gids(v: VehicleResult | None) -> set[str]:
    """All enumerated child GIDs (matched + edges).

    Legacy semantics: canary membership checks ran against the FULL children
    list, not the keyword-matched subset. Edges carry every enumerated child
    GID, so the union reproduces the legacy population even when the
    keep-filter excludes generic-description children.
    """
    if v is None:
        return set()
    kids = set(v.keyword_matched_child_gids or [])
    kids.update(e.child_gid for e in (v.edges or []) if e.child_gid)
    return kids


def evaluate_canary(canary: dict[str, Any], *,
                    vehicles_by_piid: dict[str, VehicleResult],
                    records_by_gid: dict[str, AwardRecord],
                    tiers_by_gid: dict[str, str],
                    classify_channel: Callable[[str], str]) -> GateResult:
    cid, kind = canary["id"], canary["kind"]
    if kind == "vehicle_children_count":
        v = vehicles_by_piid.get(canary["parent_piid"])
        kids = _all_enumerated_gids(v)
        ok = (v is not None and v.child_count == canary["expected_child_count"]
              and (not canary.get("require_parity") or v.parity_ok)
              and set(canary.get("required_child_gids", [])) <= kids)
        return _gate(cid, bool(ok),
                     f"children={(v.child_count if v else None)}/{canary['expected_child_count']} "
                     f"parity={(v.parity_ok if v else None)}")
    if kind == "vehicle_children_set":
        parts: list[str] = []
        ok = True
        for spec in canary["vehicles"]:
            v = vehicles_by_piid.get(spec["parent_piid"])
            kids = _all_enumerated_gids(v)
            if "required_child_gids" in spec:
                need = set(spec["required_child_gids"]) <= kids
            else:
                segs = {g.split("_")[2] for g in kids if len(g.split("_")) > 2}
                need = set(spec.get("required_child_piid_segments", [])) <= segs
            ok = ok and need
            parts.append(f"{spec['parent_piid']}: {need}")
        return _gate(cid, bool(ok), "; ".join(parts))
    if kind == "bundled_tier_burn":
        present = all(g in records_by_gid for g in canary["required_gids"])
        tiers_ok = all(tiers_by_gid.get(g) == canary["expected_tier"]
                       for g in canary["required_gids"])
        burn = next((v.ceiling_burn for v in vehicles_by_piid.values()
                     if v.parent_piid == canary["parent_piid"]), None)
        ok = present and tiers_ok and (burn or 0) >= canary.get("min_burn", 0)
        return _gate(cid, bool(ok), f"present={present} tiers={tiers_ok} burn={burn}")
    if kind == "oem_direct":
        frag = canary.get("expected_name_fragment", "")
        parts: list[str] = []
        ok = True
        for g in canary["gids"]:
            rec = records_by_gid.get(g)
            name = (rec.recipient_name if rec else "") or ""
            name_ok = bool(frag) and frag.lower() in name.lower()
            ch = classify_channel(g)
            chan_ok = ch == canary["expected_channel"]
            ok = ok and name_ok and chan_ok
            parts.append(f"{g}: name={name_ok} channel={ch}")
        return _gate(cid, bool(ok), "; ".join(parts))
    if kind == "collision_straddle":
        a, b = tiers_by_gid.get(canary["gid_a"]), tiers_by_gid.get(canary["gid_b"])
        s = records_by_gid.get(canary["straddle_gid"])
        in_w = s.window.window_net_obligations if s else None
        life = s.lifetime_obligated if s else None
        ok = (a is not None and b is not None and a != b and s is not None and life
              and life > 0 and in_w is not None and 0 < in_w < life)
        return _gate(cid, bool(ok), f"tiers={a}/{b} straddle_in={in_w} life={life}")
    return _gate(cid, False, f"unknown canary kind {kind}")


def run_all_gates(*, manifest: Any, stage1: Stage1Result,
                  records: list[AwardRecord],
                  adjs: dict[str, Adjudication],
                  vehicles: list[VehicleResult],
                  matrix: Stage6Result,
                  subawards: list[Any] | None = None) -> list[GateResult]:
    return [
        at_00_scope_lock(manifest),
        at_01_pagination_parity(stage1),
        at_02_idv_parity(vehicles),
        at_03_identity(records),
        at_04_provenance(adjs, records, manifest.is_conglomerate,
                         manifest.allow_conglomerate_recipient_search),
        at_05_bundled_segregation(
            adjs, sibling_terms=getattr(manifest, "sibling_terms", None)),
        at_06_hydration_completeness(records, subawards=subawards, matrix=matrix),
        at_07_ghost_accounting(records),
        at_08_cohort_invariants(matrix),
        at_09_taxonomy_watch(records),
        at_10_embargo_watch(vehicles, manifest.window_end),
    ]
