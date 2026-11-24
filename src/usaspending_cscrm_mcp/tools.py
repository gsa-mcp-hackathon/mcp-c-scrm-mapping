"""Ten public MCP tools (§5) — thin wrappers over pipeline Stages 0–6."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastmcp import FastMCP
from pydantic import ValidationError

from usaspending_cscrm_mcp.certification import (
    GateResult,
    issue_certificate,
    sign_certificate,
    signing_key_from_env,
)
from usaspending_cscrm_mcp.client import USAspendingClient
from usaspending_cscrm_mcp.evidence import content_hash
from usaspending_cscrm_mcp.execution import BudgetTracker, RunBudgets
from usaspending_cscrm_mcp.gates import (
    default_canaries_config,
    evaluate_canary,
    run_all_gates,
)
from usaspending_cscrm_mcp.models import AwardRecord
from usaspending_cscrm_mcp.pipeline.stage0_triage import TargetManifest, triage_target
from usaspending_cscrm_mcp.pipeline.stage1_search import run_stage1
from usaspending_cscrm_mcp.pipeline.stage2_hydrate import hydrate_awards
from usaspending_cscrm_mcp.pipeline.stage3_idv import VehicleResult, traverse_vehicle
from usaspending_cscrm_mcp.pipeline.stage4_adjudicate import (
    Adjudication,
    adjudicate,
    apply_display,
    term_re,
    to_evidence,
)
from usaspending_cscrm_mcp.pipeline.stage5_subawards import run_stage5
from usaspending_cscrm_mcp.pipeline.stage6_matrix import (
    build_matrix,
    check_completeness,
)
from usaspending_cscrm_mcp.recipient_graph import classify_channel, resolve_trees

MAX_GIDS = 500
MAX_TERMS = 50
MAX_UEIS = 100
MAX_LEDGER_ROWS = 2000


def _ledger_row(rec: AwardRecord, adj: Adjudication | None,
                manifest: TargetManifest) -> dict[str, Any]:
    """Compact report row joining a hydrated record with its adjudication."""
    return {
        "gid": rec.gid, "piid": rec.piid,
        "recipient_name": rec.recipient_name, "recipient_uei": rec.recipient_uei,
        "awarding_agency": rec.awarding_agency,
        "funding_agency": rec.funding_agency,
        "naics_code": rec.naics_code, "psc_code": rec.psc_code,
        "start_date": rec.start_date, "end_date": rec.end_date,
        "lifetime_obligated": str(rec.lifetime_obligated),
        "window_net": str(rec.window.window_net_obligations),
        "window_positive": str(rec.window.window_positive_obligations),
        "window_negative": str(rec.window.window_negative_deobligations),
        "window_txn_count": rec.window.window_transaction_count,
        "window_activity": rec.window.window_activity_status,
        "lifecycle": rec.award_lifecycle_status,
        "ghost": rec.ghost_historical, "ghost_reason": rec.historical_reason,
        "assisted": rec.assisted_classification,
        "tier": adj.tier if adj else "UNADJUDICATED",
        "display": adj.display if adj else "UNADJUDICATED",
        "rule": adj.rule if adj else "",
        "basis": adj.basis if adj else "vendor_associated",
        "snippet": (adj.snippet if adj else "")[:240],
        "dedicated_lineage": adj.dedicated_lineage if adj else False,
    }


@dataclass
class ServerDeps:
    client_factory: Callable[[], USAspendingClient] = USAspendingClient
    budgets: RunBudgets = field(default_factory=RunBudgets)
    signing_key: bytes | None = None
    caller_id: str = "local"

    def client(self) -> USAspendingClient:
        return self.client_factory()


_DEPS = ServerDeps()


def configure(deps: ServerDeps) -> None:
    global _DEPS
    _DEPS = deps


def _manifest_of(manifest: dict[str, Any]) -> TargetManifest:
    try:
        return TargetManifest.model_validate(manifest)
    except ValidationError as exc:
        raise ValueError(f"invalid target_manifest: {exc}") from exc


def _check_sizes(*, gids: list | None = None, terms: list | None = None,
                 ueis: list | None = None) -> None:
    if gids is not None and len(gids) > MAX_GIDS:
        raise ValueError(f"award_gids exceeds limit {MAX_GIDS}")
    if terms is not None and len(terms) > MAX_TERMS:
        raise ValueError(f"terms exceed limit {MAX_TERMS}")
    if ueis is not None and len(ueis) > MAX_UEIS:
        raise ValueError(f"ueis exceed limit {MAX_UEIS}")


def _budgets(overrides: dict[str, Any] | None) -> RunBudgets:
    if not overrides:
        return _DEPS.budgets
    try:
        return RunBudgets(**overrides)
    except ValidationError as exc:
        raise ValueError(f"invalid budgets: {exc}") from exc


async def _close(client: USAspendingClient) -> None:
    with contextlib.suppress(Exception):  # noqa: BLE001 - close is best-effort
        await client.aclose()


def register_tools(mcp: FastMCP) -> None:
    @mcp.tool(name="target_triage")
    async def target_triage(target_query: str, candidate_terms: list[str],
                            verified_oem_ueis: list[str] | None = None,
                            target_archetype: str = "ARCHETYPE_1_PURE_PLAY_VENDOR",
                            product_terms: list[str] | None = None,
                            negative_terms: list[str] | None = None,
                            domain_nouns: list[str] | None = None,
                            from_date: str = "2023-10-01", to_date: str = "2026-09-30",
                            continuation: str | None = None) -> dict[str, Any]:
        """Stage 0: probe candidate terms and lock the target manifest."""
        _check_sizes(terms=candidate_terms)
        client = _DEPS.client()
        try:
            out = await triage_target(
                client, target_name=target_query, candidate_terms=candidate_terms,
                verified_oem_ueis=verified_oem_ueis or [],
                target_archetype=target_archetype, product_terms=product_terms,
                negative_terms=negative_terms, domain_nouns=domain_nouns,
                from_date=from_date, to_date=to_date,
                budgets=_DEPS.budgets, signing_key=_DEPS.signing_key,
                continuation=continuation, caller_id=_DEPS.caller_id)
        finally:
            await _close(client)
        manifest = out.get("manifest")
        return {"status": out["status"], "manifest": manifest.model_dump(mode="json")
                if manifest is not None else None,
                "manifest_hash": out.get("manifest_hash"),
                "continuation": out.get("continuation")}

    @mcp.tool(name="award_search")
    async def award_search(target_manifest: dict[str, Any],
                           budgets: dict[str, Any] | None = None,
                           continuation: str | None = None) -> dict[str, Any]:
        """Stage 1: dual-vector search governed by the locked manifest."""
        manifest = _manifest_of(target_manifest)
        client = _DEPS.client()
        try:
            res = await run_stage1(client, manifest, _budgets(budgets),
                                   _DEPS.signing_key, continuation, _DEPS.caller_id)
        finally:
            await _close(client)
        return res.model_dump(mode="json")

    @mcp.tool(name="agency_exposure_search")
    async def agency_exposure_search(target_manifest: dict[str, Any],
                                    budgets: dict[str, Any] | None = None,
                                    continuation: str | None = None) -> dict[str, Any]:
        """Stage 1 hits grouped by awarding/funding agency with parity."""
        manifest = _manifest_of(target_manifest)
        client = _DEPS.client()
        try:
            res = await run_stage1(client, manifest, _budgets(budgets),
                                   _DEPS.signing_key, continuation, _DEPS.caller_id)
        finally:
            await _close(client)
        by_agency: dict[str, dict[str, Any]] = {}
        for h in res.hits:
            for agency in {h.awarding_agency or "UNKNOWN", h.funding_agency or "UNKNOWN"}:
                entry = by_agency.setdefault(agency, {"hits": 0, "gids": []})
                entry["hits"] += 1
                if len(entry["gids"]) < 25:
                    entry["gids"].append(h.gid)
        return {"status": res.status, "agencies": by_agency,
                "observations": [o.model_dump(mode="json") for o in res.observations],
                "gated_filtered_out": res.gated_filtered_out,
                "continuation": res.continuation,
                "incomplete_reason": res.incomplete_reason}

    @mcp.tool(name="award_context")
    async def award_context(award_gids: list[str], window_start: str = "2023-10-01",
                            window_end: str = "2026-09-30",
                            budgets: dict[str, Any] | None = None,
                            continuation: str | None = None) -> dict[str, Any]:
        """Stage 2: full hydration with transaction-basis windows."""
        _check_sizes(gids=award_gids)
        client = _DEPS.client()
        try:
            return await hydrate_awards(
                client, award_gids, window_start, window_end, _budgets(budgets),
                _DEPS.signing_key, continuation, _DEPS.caller_id)
        finally:
            await _close(client)

    @mcp.tool(name="award_narrative_fetch")
    async def award_narrative_fetch(award_gids: list[str],
                                    window_start: str = "2023-10-01",
                                    window_end: str = "2026-09-30") -> dict[str, Any]:
        """Stage 2 projection: description texts only (base + transactions)."""
        _check_sizes(gids=award_gids)
        client = _DEPS.client()
        try:
            out = await hydrate_awards(client, award_gids, window_start, window_end,
                                       _DEPS.budgets, None, None, _DEPS.caller_id)
        finally:
            await _close(client)
        narratives = [{"gid": r.gid, "base_description": r.base_description,
                       "transaction_descriptions": r.transaction_descriptions}
                      for r in out.get("hydrated", []) if isinstance(r, AwardRecord)]
        return {"status": out["status"], "narratives": narratives,
                "failed": [f.model_dump(mode="json") for f in out.get("failed", [])]}

    @mcp.tool(name="vehicle_hierarchy")
    async def vehicle_hierarchy(parent_gids: list[str], standalone_terms: list[str],
                                gated_terms: list[str] | None = None,
                                force_vehicle_class: dict[str, str] | None = None,
                                budgets: dict[str, Any] | None = None) -> dict[str, Any]:
        """Stage 3: traverse IDV parents with intrinsic homogeneity gating."""
        _check_sizes(gids=parent_gids,
                     terms=(standalone_terms or []) + (gated_terms or []))
        terms = standalone_terms + (gated_terms or [])
        rxes = [term_re(t) for t in terms if t]
        keep = lambda d: any(rx.search(d or "") for rx in rxes)  # noqa: E731
        client = _DEPS.client()
        tracker = BudgetTracker(_budgets(budgets))
        try:
            vehicles: list[VehicleResult | None] = []
            for gid in parent_gids:
                if tracker.exhausted():
                    return {"status": "INCOMPLETE", "reason": tracker.exhausted(),
                            "vehicles": [v.model_dump(mode="json") for v in vehicles
                                         if v is not None]}
                vehicles.append(await traverse_vehicle(
                    client, gid, keep,
                    (force_vehicle_class or {}).get(gid),
                    budgets=_budgets(budgets), tracker=tracker))
        finally:
            await _close(client)
        return {"status": "COMPLETE",
                "vehicles": [v.model_dump(mode="json") for v in vehicles
                             if v is not None]}

    @mcp.tool(name="recipient_graph")
    async def recipient_graph(ueis: list[str], verified_oem_ueis: list[str] | None = None,
                              homograph_ueis: list[str] | None = None,
                              oem_name_tokens: list[str] | None = None,
                              oem_name: str | None = None) -> dict[str, Any]:
        """Recipient children trees with structural channel classification."""
        _check_sizes(ueis=ueis)
        client = _DEPS.client()
        try:
            nodes = await resolve_trees(
                client, ueis, verified_ueis=set(verified_oem_ueis or []),
                homograph_ueis=set(homograph_ueis or []),
                oem_name_tokens=tuple(oem_name_tokens or []), oem_name=oem_name)
        finally:
            await _close(client)
        return {"nodes": [n.model_dump(mode="json") for n in nodes]}

    @mcp.tool(name="lexical_review")
    async def lexical_review(records: list[dict[str, Any]],
                             standalone_terms: list[str],
                             gated_terms: list[str] | None = None,
                             verified_oem_ueis: list[str] | None = None,
                             homograph_ueis: list[str] | None = None,
                             domain_context_regex: str | None = None,
                             competitor_regex: str | None = None,
                             is_conglomerate: bool = False,
                             sibling_terms: list[str] | None = None,
                             target_name: str = "",
                             dedicated_lineage_gids: list[str] | None = None
                             ) -> dict[str, Any]:
        """Stage 4: adjudicate hydrated records with typed evidence."""
        _check_sizes(terms=standalone_terms + (gated_terms or []))
        dedicated = set(dedicated_lineage_gids or [])
        out: list[dict[str, Any]] = []
        for raw in records:
            try:
                rec = AwardRecord.model_validate(raw)
            except ValidationError as exc:
                out.append({"gid": raw.get("gid", "?"), "tier": "DROPPED",
                            "rule": f"INVALID_RECORD:{exc}", "display": "DROPPED"})
                continue
            adj = apply_display(adjudicate(
                rec, standalone_terms=standalone_terms,
                gated_terms=gated_terms or [],
                verified_ueis=set(verified_oem_ueis or []),
                homograph_ueis=set(homograph_ueis or []),
                domain_context_rx=domain_context_regex,
                competitor_rx=competitor_regex, is_conglomerate=is_conglomerate,
                sibling_terms=sibling_terms or [],
                target_name=target_name,
                dedicated_lineage=rec.gid in dedicated), rec)
            ev = to_evidence(adj, rec)
            entry = adj.model_dump(mode="json")
            entry["evidence"] = ev.model_dump(mode="json")
            out.append(entry)
        return {"adjudications": out}

    @mcp.tool(name="subaward_sweep")
    async def subaward_sweep(prime_gids: list[str], sweep_terms: list[str] | None = None,
                             standalone_terms: list[str] | None = None,
                             gated_terms: list[str] | None = None,
                             verified_oem_ueis: list[str] | None = None,
                             homograph_ueis: list[str] | None = None,
                             is_conglomerate: bool = False,
                             sibling_terms: list[str] | None = None,
                             target_name: str = "",
                             budgets: dict[str, Any] | None = None) -> dict[str, Any]:
        """Stage 5: segregated subaward ledger + adjudicated sweep."""
        _check_sizes(gids=prime_gids, terms=(sweep_terms or []))
        client = _DEPS.client()
        try:
            res = await run_stage5(
                client, prime_gids, sweep_terms=sweep_terms,
                standalone_terms=standalone_terms, gated_terms=gated_terms,
                verified_ueis=set(verified_oem_ueis or []),
                homograph_ueis=set(homograph_ueis or []),
                is_conglomerate=is_conglomerate, budgets=_budgets(budgets),
                sibling_terms=sibling_terms or [], target_name=target_name)
        finally:
            await _close(client)
        return res.model_dump(mode="json")

    @mcp.tool(name="audit_run")
    async def audit_run(target_manifest: dict[str, Any],
                        sweep_terms: list[str] | None = None,
                        include_subawards: bool = True,
                        run_canaries: bool = False,
                        canaries_config: dict[str, Any] | None = None,
                        budgets: dict[str, Any] | None = None,
                        continuations: dict[str, str] | None = None
                        ) -> dict[str, Any]:
        """Stages 1–6 + AT-00..AT-10 (+ optional live canaries): signed certificate."""
        manifest = _manifest_of(target_manifest)
        conts = continuations or {}
        b = _budgets(budgets)
        client = _DEPS.client()
        try:
            s1 = await run_stage1(client, manifest, b, _DEPS.signing_key,
                                   conts.get("stage1"), _DEPS.caller_id)
            if s1.status != "COMPLETE":
                return {"status": "INCOMPLETE", "stage": "stage1",
                        "continuations": {"stage1": s1.continuation}}
            hyd = await hydrate_awards(client, [h.gid for h in s1.hits],
                                       manifest.window_start, manifest.window_end,
                                       b, _DEPS.signing_key, conts.get("stage2"),
                                       _DEPS.caller_id)
            if hyd["status"] != "COMPLETE":
                return {"status": "INCOMPLETE", "stage": "stage2",
                        "continuations": {"stage2": hyd["continuation"]}}
            records: list[AwardRecord] = hyd["hydrated"]
            by_gid = {r.gid: r for r in records}
            canaries_cfg: dict[str, Any] | None = (
                canaries_config or default_canaries_config()) if run_canaries else None
            if canaries_cfg:
                probe_gids = [g for g in canaries_cfg.get("probe_gids", [])
                              if g not in by_gid]
                if probe_gids:
                    probe_out = await hydrate_awards(
                        client, probe_gids, manifest.window_start,
                        manifest.window_end, b, None, None, _DEPS.caller_id)
                    for r in probe_out["hydrated"]:
                        records.append(r)
                        by_gid[r.gid] = r
                    hyd["failed"].extend(probe_out["failed"])
            parents = sorted({r.parent_gid for r in records if r.parent_gid})
            rxes = [term_re(t) for t in
                    manifest.standalone_terms + manifest.context_gated_terms if t]
            keep = lambda d: any(rx.search(d or "") for rx in rxes)  # noqa: E731
            tracker = BudgetTracker(b)
            dedicated_children: set[str] = set()
            vehicles: list[VehicleResult] = []
            for gid in parents:
                if tracker.exhausted():
                    break
                veh = await traverse_vehicle(client, gid, keep, budgets=b,
                                             tracker=tracker)
                if veh is None:
                    continue
                vehicles.append(veh)
                if veh.classification == "DEDICATED_SINGLE_PURPOSE_IDV":
                    dedicated_children.update(veh.keyword_matched_child_gids)
            extra = [g for g in dedicated_children if g not in by_gid]
            if extra:
                hyd2 = await hydrate_awards(client, extra, manifest.window_start,
                                            manifest.window_end, b, None, None,
                                            _DEPS.caller_id)
                for r in hyd2["hydrated"]:
                    records.append(r)
                    by_gid[r.gid] = r
            adjs: dict[str, Adjudication] = {}
            for r in records:
                adjs[r.gid] = apply_display(adjudicate(
                    r, standalone_terms=manifest.standalone_terms,
                    gated_terms=manifest.context_gated_terms,
                    verified_ueis=set(manifest.verified_oem_ueis),
                    homograph_ueis=set(manifest.excluded_homograph_ueis),
                    domain_context_rx=manifest.positive_regex or None,
                    competitor_rx=manifest.negative_regex or None,
                    is_conglomerate=manifest.is_conglomerate,
                    sibling_terms=manifest.sibling_terms,
                    target_name=manifest.target_name,
                    dedicated_lineage=r.gid in dedicated_children), r)
            gate_subawards: list[Any] = []
            s5 = None
            if include_subawards:
                tier1 = [g for g, a in adjs.items() if a.tier in ("Tier-1A", "Tier-1B")]
                sweep_base = list(sweep_terms or manifest.standalone_terms)
                for sib in manifest.sibling_terms:
                    if sib not in sweep_base:
                        sweep_base.append(sib)
                if not sweep_base:
                    # Gated-only manifest (e.g. vendor query with no
                    # standalone term): sweep the gated brand terms so Tier-2
                    # subaward recall is not silently zero.
                    sweep_base = list(manifest.context_gated_terms)
                s5res = await run_stage5(
                    client, tier1, sweep_terms=sweep_base,
                    standalone_terms=manifest.standalone_terms,
                    gated_terms=manifest.context_gated_terms,
                    verified_ueis=set(manifest.verified_oem_ueis),
                    homograph_ueis=set(manifest.excluded_homograph_ueis),
                    is_conglomerate=manifest.is_conglomerate, budgets=b,
                    sibling_terms=manifest.sibling_terms,
                    target_name=manifest.target_name)
                s5 = s5res.model_dump(mode="json")
                gate_subawards = ([s for lg in s5res.ledgers for s in lg.subawards]
                                  + list(s5res.kept_hits))
            s6 = build_matrix(records, adjs, subawards=gate_subawards)
            s6.completeness = check_completeness(
                s1.observations, len(records), hyd["failed"],
                manifest.unresolved_limitations)
            gates = run_all_gates(manifest=manifest, stage1=s1, records=records,
                                  adjs=adjs, vehicles=vehicles, matrix=s6,
                                  subawards=gate_subawards)
            canary_results: list[GateResult] = []
            if canaries_cfg:
                vehicles_by_piid = {v.parent_piid: v for v in vehicles
                                    if v.parent_piid}
                records_by_gid = {r.gid: r for r in records}
                tiers_by_gid = {g: a.tier for g, a in adjs.items()}
                verified = set(manifest.verified_oem_ueis)
                homographs = set(manifest.excluded_homograph_ueis)
                collisions = set(manifest.rejected_terms)

                def _chan(gid: str) -> str:
                    rec = records_by_gid.get(gid)
                    ch, _ = classify_channel(
                        rec.recipient_uei if rec else None,
                        rec.parent_recipient_uei if rec else None,
                        rec.recipient_name if rec else None,
                        verified, homographs, (), None, collisions)
                    return ch

                for can in canaries_cfg.get("canaries", []):
                    canary_results.append(evaluate_canary(
                        can, vehicles_by_piid=vehicles_by_piid,
                        records_by_gid=records_by_gid, tiers_by_gid=tiers_by_gid,
                        classify_channel=_chan))
            unresolved = list(manifest.unresolved_limitations)
            if not run_canaries:
                unresolved.append("live canaries not run (run_canaries=False)")
            signing_note = ""
            try:
                signing_key = signing_key_from_env()
            except ValueError as exc:
                signing_key = None
                signing_note = f"signing key invalid: {exc}"
            cert = issue_certificate(
                target_name=manifest.target_name, window_start=manifest.window_start,
                window_end=manifest.window_end, manifest_hash=manifest.canonical_hash(),
                matrix_hash=s6.matrix_hash, gate_results=gates,
                canary_results=canary_results, totals=s6.totals,
                limitations=manifest.unresolved_limitations,
                unresolved=unresolved + ([signing_note] if signing_note else []))
            if signing_key is not None:
                sign_certificate(cert, signing_key)
            else:
                cert.unresolved.append("certificate UNSIGNED: CSCRM_SIGNING_KEY not provisioned")
                cert.content_hash = content_hash(cert.model_dump(
                    mode="json", exclude={"content_hash", "signature"}))
            overall = (all(g.status == "PASS" for g in gates)
                       and all(c.status == "PASS" for c in canary_results))
            from decimal import Decimal as _Decimal
            ledger = [_ledger_row(by_gid.get(r.gid, r), adjs.get(r.gid), manifest)
                      for r in records]
            ledger.sort(key=lambda e: _Decimal(e.get("window_net") or "0"),
                        reverse=True)
            ledger_truncated = False
            if len(ledger) > MAX_LEDGER_ROWS:
                ledger, ledger_truncated = ledger[:MAX_LEDGER_ROWS], True
        finally:
            await _close(client)
        return {"status": "COMPLETE", "overall": "PASS" if overall else "FAIL",
                "matrix": s6.model_dump(mode="json"),
                "ledger": ledger, "ledger_truncated": ledger_truncated,
                "gates": [g.model_dump(mode="json") for g in gates],
                "canaries": [c.model_dump(mode="json") for c in canary_results],
                "subawards": s5,
                "certificate": cert.model_dump(mode="json")}
