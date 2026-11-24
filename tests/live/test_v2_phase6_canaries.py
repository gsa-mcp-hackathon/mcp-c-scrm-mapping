"""Phase 6 live canary execution (read-only; skips only when inputs/network absent).

- Ground-truth sources (all documented, none fabricated):
- canaries.json: the five canary definitions + seven probe GIDs.
- Legacy Stage-0 manifest (scratch, Oct 27): standalone/gated terms + regexes.
- canary_4 UEIs: observed live on the canary GIDs themselves (provenance in
  canaries.json); the gate additionally requires JOHNSON CONTROLS lineage
  in the live recipient names, so a UEI rotation fails loudly instead of
  passing tautologically.
"""

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from usaspending_cscrm_mcp.client import TransportError, USAspendingClient
from usaspending_cscrm_mcp.execution import RunBudgets
from usaspending_cscrm_mcp.gates import (
    competitor_regex_from_terms,
    default_canaries_config,
    evaluate_canary,
)
from usaspending_cscrm_mcp.pipeline.stage2_hydrate import hydrate_awards
from usaspending_cscrm_mcp.pipeline.stage3_idv import traverse_vehicle
from usaspending_cscrm_mcp.pipeline.stage4_adjudicate import (
    adjudicate,
    apply_display,
    term_re,
)
from usaspending_cscrm_mcp.recipient_graph import classify_channel

LEGACY_MANIFEST = Path(
    "C:/Users/User/AppData/Local/hermes/cache/scratch/cscrm_stage0_manifest.json")
WINDOW = ("2023-10-01", "2026-12-31")


def load_terms():
    if not LEGACY_MANIFEST.exists():
        pytest.skip("legacy Stage-0 manifest absent (scratch pruned?)")
    m = json.loads(LEGACY_MANIFEST.read_text())
    return (m.get("standalone_keywords", []), m.get("context_gated_keywords", []),
            m.get("positive_domain_context_regex"),
            m.get("competitor_bundle_regex"))


async def parent_gid_for_piid(client, piid):
    """Resolve a parent PIID to its GID via PIID-as-keyword search.

    Contract and IDV code groups are queried separately: the API 422s
    mixed sets on spending_by_award (verified live). Canary specs carry
    modification-suffixed IDs (e.g. ``11316023A0004EOP_1100``); the award
    PIID is the bare prefix, so the lookup strips a trailing ``_<mod>``
    segment — the same normalization the v1 harness applies.
    """
    lookup = piid.rsplit("_", 1)[0] if "_" in piid else piid
    for codes in (["A", "B", "C", "D"],
                  ["IDV_A", "IDV_B", "IDV_C", "IDV_D", "IDV_E"]):
        try:
            data = await client.post("search/spending_by_award/", {
                "filters": {"keywords": [lookup], "award_type_codes": codes},
                "fields": ["Award ID", "generated_internal_id"],
                "sort": "Award ID", "order": "asc", "limit": 25, "page": 1})
        except TransportError as exc:
            if exc.is_transient:
                pytest.skip(f"live API unavailable during parent resolution: {exc}")
            raise
        for row in data.get("results", []) or []:
            if isinstance(row, dict) and row.get("Award ID") == lookup:
                return row.get("generated_internal_id")
    return None


@pytest.mark.asyncio
async def test_live_canaries_all_pass(tmp_path):
    standalone, gated, domain_rx, competitor_rx = load_terms()
    assert standalone, "legacy manifest has no standalone terms"
    cfg = default_canaries_config()
    client = USAspendingClient(timeout=60.0, max_concurrency=4)
    try:
        try:
            out = await hydrate_awards(
                client, list(cfg["probe_gids"]), *WINDOW,
                budgets=RunBudgets(max_pages=300, max_awards=50,
                                   overall_deadline_s=1500.0))
        except TransportError as exc:
            if exc.is_transient:
                pytest.skip(f"live API unavailable mid-hydration: {exc}")
            raise
        records = {r.gid: r for r in out["hydrated"]}
        missing = [g for g in cfg["probe_gids"] if g not in records]
        assert not missing, f"probe hydration failed: {missing} " \
            f"faults={[ (f.gid, f.error_category) for f in out['failed'] ]}"
        rxes = [term_re(t) for t in standalone + gated if t]
        # EXPERIMENT B: domain-aware child matching — manifest brand terms OR
        # the manifest's positive domain-context regex (same signal
        # adjudication already uses). Competitor exclusion stays in
        # adjudication; traversal only confirms domain scope.
        domain_res = [re.compile(domain_rx, re.IGNORECASE)] if domain_rx else []

        def keep(d):
            t = d or ""
            return (any(rx.search(t) for rx in rxes)
                    or any(rx.search(t) for rx in domain_res))
        canary_parents = [c["parent_piid"]
                          for c in cfg["canaries"]
                          if c["kind"] in ("bundled_tier_burn",
                                           "vehicle_children_count")] \
            + [spec["parent_piid"]
               for c in cfg["canaries"] if c["kind"] == "vehicle_children_set"
               for spec in c["vehicles"]]
        resolved = {}
        for piid in dict.fromkeys(canary_parents):
            gid = await parent_gid_for_piid(client, piid)
            if not gid:
                # Fallback: a hydrated child already proves the parent GID
                # via lineage (keyword search intermittently misses live
                # records, e.g. 75F40124D00013, verified live 2026-11-01).
                gid = next((r.parent_gid for r in records.values()
                            if r.parent_gid and piid in r.parent_gid), None)
            if gid:
                resolved[piid] = gid
        missing_piids = [p for p in dict.fromkeys(canary_parents) if p not in resolved]
        assert not missing_piids, f"canary parent PIIDs unresolvable live: {missing_piids}"
        parents = sorted({r.parent_gid for r in records.values() if r.parent_gid}
                         | set(resolved.values()))
        vehicles, dedicated_children = [], set()
        for gid in parents:
            try:
                veh = await traverse_vehicle(client, gid, keep)
            except TransportError as exc:
                if exc.is_transient:
                    pytest.skip(f"live API unavailable mid-traversal: {exc}")
                raise
            if veh is None:
                continue
            vehicles.append(veh)
            if veh.classification == "DEDICATED_SINGLE_PURPOSE_IDV":
                dedicated_children.update(veh.keyword_matched_child_gids)
        adjs, tiers = {}, {}
        hhs_terms = next((c.get("competitor_terms") for c in cfg["canaries"]
                          if c["id"] == "canary_3_hhs_bundled"), None)
        competitor_rx = (competitor_regex_from_terms(hhs_terms)
                         or domain_rx or competitor_rx)
        for gid, rec in records.items():
            adj = apply_display(adjudicate(
                rec, standalone_terms=standalone, gated_terms=gated,
                verified_ueis=set(), homograph_ueis=set(),
                domain_context_rx=domain_rx, competitor_rx=competitor_rx,
                dedicated_lineage=gid in dedicated_children), rec)
            adjs[gid] = adj
            tiers[gid] = adj.tier
        # canary_4 verified graph comes from the canary config (provenance
        # documented there); the gate itself requires JCI lineage in the
        # live recipient names, so stale UEIs fail instead of passing.
        can4 = next(c for c in cfg["canaries"] if c["id"] == "canary_4_oem_direct")
        verified = set(can4.get("verified_ueis", []))
        assert verified, "canary_4 must document its verified UEI set"

        def _chan(gid: str) -> str:
            rec = records.get(gid)
            ch, _ = classify_channel(
                rec.recipient_uei if rec else None, None, None,
                verified, set(), (), None, set())
            return ch

        def _bare(p: str) -> str:
            # Traverse keys vehicles by the award PIID (bare); canary specs
            # carry modification-suffixed IDs — normalize both sides.
            return p.rsplit("_", 1)[0] if "_" in str(p) else p

        def _norm_can(can: dict[str, Any]) -> dict[str, Any]:
            can = dict(can)
            if "parent_piid" in can:
                can["parent_piid"] = _bare(can["parent_piid"])
            if "vehicles" in can:
                can["vehicles"] = [{**s, "parent_piid": _bare(s.get("parent_piid", ""))}
                                   for s in can["vehicles"]]
            return can

        results = [evaluate_canary(
            _norm_can(can),
            vehicles_by_piid={_bare(v.parent_piid): v for v in vehicles
                              if v.parent_piid},
            records_by_gid=records, tiers_by_gid=tiers,
            classify_channel=_chan) for can in cfg["canaries"]]
        artifact = {
            "ran_at": datetime.now(UTC).isoformat(),
            "window": list(WINDOW),
            "canaries": [r.model_dump(mode="json") for r in results],
            "tiers": tiers,
        }
        evals = Path("C:/Users/User/projects/cscrm-usa-spending-mcp/evals")
        evals.mkdir(exist_ok=True)
        stamp = date.today().isoformat()
        (evals / f"live_canaries_{stamp}.json").write_text(json.dumps(artifact, indent=2))
        failed = [(r.gate_id, r.detail) for r in results if r.status != "PASS"]
        assert not failed, f"live canary failures: {failed}"
    finally:
        await client.aclose()
