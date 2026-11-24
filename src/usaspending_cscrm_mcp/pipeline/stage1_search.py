"""Stage 1 — dual-vector exhaustive search (manifest-governed, parity-checked).

- STANDALONE terms searched as-is; CONTEXT_GATED terms retrieved by keyword
  then filtered by the gated rule (sibling/delivery noun after boilerplate
  masking); REJECTED terms never dispatched.
- Contract (A/B/C/D) and IDV (IDV_A..IDV_E) groups queried separately
  (mixed groups 422). Pagination continues while has_next AND budget allows;
  terminal page state recorded; incomplete populations flagged, never padded.
- Awards keyed by generated_internal_id; duplicates collapse by GID.
"""

from __future__ import annotations

import re as _re
from typing import Any

from pydantic import BaseModel, Field

from usaspending_cscrm_mcp.client import USAspendingClient
from usaspending_cscrm_mcp.execution import (
    BudgetTracker,
    ContinuationToken,
    RunBudgets,
    check_scope_binding,
    mint_continuation,
    new_run_id,
)
from usaspending_cscrm_mcp.models import QueryObservation
from usaspending_cscrm_mcp.pipeline.stage0_triage import (
    SAMPLE_FIELDS,
    SIBLING_NOUNS,
    TargetManifest,
    strip_trigger,
)

CONTRACT_CODES = ["A", "B", "C", "D"]
IDV_CODES = ["IDV_A", "IDV_B", "IDV_C", "IDV_D", "IDV_E"]

_MASK_RES = (
    _re.compile(r"\bFAR\b.{0,80}?(?:construction|janitorial|custodial)\b", _re.I),
    _re.compile(r"\b52\.211-10\b"),
)


class SearchHit(BaseModel):
    gid: str
    vector: str
    vector_kind: str
    award_group: str
    piid: str = ""
    recipient_name: str = ""
    recipient_uei: str = ""
    awarding_agency: str = ""
    funding_agency: str = ""
    award_amount: str = ""
    description: str = ""


class Stage1Result(BaseModel):
    status: str = "COMPLETE"
    run_id: str = ""
    hits: list[SearchHit] = Field(default_factory=list)
    observations: list[QueryObservation] = Field(default_factory=list)
    gated_filtered_out: int = 0
    incomplete_reason: str | None = None
    continuation: str | None = None


def gated_keep(row: dict[str, Any], term: str,
               domain_nouns: tuple[str, ...] = ()) -> bool:
    """Gated match: after trigger-strip + boilerplate mask, a sibling or
    caller-supplied domain noun must co-occur. Delivery-only nouns never
    confirm. The noun set MUST match Stage-0 scoring (SIBLING_NOUNS plus the
    manifest's domain_nouns) — a narrower set here silently voids triage."""
    text = " ".join(str(row.get(f, "") or "") for f in
                    ("Description", "NAICS Description", "PSC Description"))
    stripped = strip_trigger(text, term)
    for rx in _MASK_RES:
        stripped = rx.sub(" ", stripped)
    low = stripped.lower()
    nouns = set(SIBLING_NOUNS) | {n.lower() for n in domain_nouns if n}
    return any(n in low for n in nouns)


def row_to_hit(row: dict[str, Any], vector: str, kind: str, group: str) -> SearchHit | None:
    gid = str(row.get("generated_internal_id", "") or "")
    if not gid:
        return None
    return SearchHit(
        gid=gid, vector=vector, vector_kind=kind, award_group=group,
        piid=str(row.get("Award ID", "") or ""),
        recipient_name=str(row.get("Recipient Name", "") or ""),
        recipient_uei=str(row.get("Recipient UEI", "") or ""),
        awarding_agency=str(row.get("Awarding Agency", "") or ""),
        funding_agency=str(row.get("Funding Agency", "") or ""),
        award_amount=str(row.get("Award Amount", "") or ""),
        description=str(row.get("Description", "") or "")[:500],
    )


async def reported_total(client: USAspendingClient, term: str, group: str,
                         manifest: TargetManifest,
                         tracker: BudgetTracker) -> int | None:
    """Parity anchor: count endpoint with identical filters. None if unavailable."""
    try:
        if tracker.exhausted():
            return None
        tracker.pages += 1
        data = await client.post(
            "search/spending_by_award_count/",
            {"filters": {
                "keywords": [term],
                "award_type_codes": CONTRACT_CODES if group == "contracts" else IDV_CODES,
                "time_period": [{"start_date": manifest.window_start,
                                 "end_date": manifest.window_end}]}})
        results = data.get("results", {}) or {}
        key = "contracts" if group == "contracts" else "idvs"
        total = results.get(key)
        return int(total) if total is not None else None
    except Exception:  # noqa: BLE001 - parity anchor advisory; pages still verify
        return None


async def search_vector(client: USAspendingClient, term: str, kind: str,
                        group: str, manifest: TargetManifest,
                        budgets: RunBudgets, tracker: BudgetTracker,
                        start_page: int, run_id: str,
                        seen: set[str]) -> tuple[list[SearchHit], QueryObservation | None,
                                                int, int, str | None]:
    """Paginate one (term, group) query. Returns (hits, observation, gated_out,
    next_page_or_-1, reason). One observation per query: complete only when the
    terminal page is reached AND fetched == count-endpoint total."""
    query_id = f"{kind}:{term}:{group}"
    reported = await reported_total(client, term, group, manifest, tracker)
    hits: list[SearchHit] = []
    gated_out = 0
    fetched = 0
    page = start_page
    while True:
        if tracker.exhausted():
            return hits, QueryObservation(
                query_id=query_id, endpoint="search/spending_by_award/",
                canonical_filter_hash="", reported_count=reported if reported is not None else -1,
                fetched_count=fetched, page_size=25, terminal_has_next=True,
                complete=False), gated_out, page, tracker.exhausted()
        if page > 1 and (page - 1) * 25 >= budgets.max_awards:
            return hits, QueryObservation(
                query_id=query_id, endpoint="search/spending_by_award/",
                canonical_filter_hash="", reported_count=reported if reported is not None else -1,
                fetched_count=fetched, page_size=25, terminal_has_next=True,
                complete=False), gated_out, -1, None
        tracker.pages += 1
        payload = {
            "filters": {
                "keywords": [term],
                "award_type_codes": CONTRACT_CODES if group == "contracts" else IDV_CODES,
                "time_period": [{"start_date": manifest.window_start,
                                 "end_date": manifest.window_end}],
            },
            "fields": SAMPLE_FIELDS,
            "limit": 25,
            "page": page,
            "sort": "Award Amount",
            "order": "desc",
        }
        try:
            data = await client.post("search/spending_by_award/", payload)
        except Exception as exc:  # noqa: BLE001 - recorded, population flagged
            return hits, QueryObservation(
                query_id=query_id, endpoint="search/spending_by_award/",
                canonical_filter_hash="", reported_count=reported if reported is not None else -1,
                fetched_count=fetched, page_size=25, terminal_has_next=True,
                complete=False), gated_out, page, f"query_error:{type(exc).__name__}"
        rows = data.get("results", []) or []
        meta = data.get("page_metadata", {}) or {}
        has_next = bool(meta.get("hasNext"))
        for row in rows:
            if not isinstance(row, dict):
                continue
            if kind == "gated" and not gated_keep(
                    row, term, tuple(manifest.domain_nouns)):
                gated_out += 1
                continue
            hit = row_to_hit(row, term, kind, group)
            if hit is None or hit.gid in seen:
                continue
            seen.add(hit.gid)
            hits.append(hit)
            tracker.awards += 1
        fetched += len(rows)
        if not has_next:
            complete = bool(reported is not None and fetched == reported)
            return hits, QueryObservation(
                query_id=query_id, endpoint="search/spending_by_award/",
                canonical_filter_hash="", reported_count=reported if reported is not None else -1,
                fetched_count=fetched, page_size=25, terminal_has_next=False,
                complete=complete), gated_out, -1, (
                    None if complete else
                    f"parity_mismatch: fetched={fetched} reported={reported}")
        page += 1


async def run_stage1(client: USAspendingClient, manifest: TargetManifest,
                     budgets: RunBudgets | None = None,
                     signing_key: bytes | None = None,
                     continuation: str | None = None,
                     caller_id: str = "local") -> Stage1Result:
    """Execute all manifest vectors. INCOMPLETE + continuation on exhaustion."""
    budgets = budgets or RunBudgets()
    tracker = BudgetTracker(budgets)
    run_id = new_run_id()
    vectors = ([(t, "standalone") for t in manifest.standalone_terms]
               + [(t, "gated") for t in manifest.context_gated_terms])
    mh = manifest.canonical_hash()
    start_vi, start_group, start_page = 0, "contracts", 1
    if continuation and signing_key:
        ct = ContinuationToken.decode(continuation, signing_key)
        check_scope_binding(ct, caller_id, "stage1:" + mh, "search")
        run_id = ct.run_id
        start_vi = int(ct.cursor.get("vi", 0))
        start_group = str(ct.cursor.get("group", "contracts"))
        start_page = int(ct.cursor.get("page", 1))
    result = Stage1Result(run_id=run_id)
    seen: set[str] = set()
    for vi in range(start_vi, len(vectors)):
        term, kind = vectors[vi]
        groups = ["contracts", "idvs"] if not (vi == start_vi and start_group == "idvs") else ["idvs"]
        for group in groups:
            page0 = start_page if (vi == start_vi and group == start_group) else 1
            start_page, start_group = 1, "contracts"
            hits, obs, gated_out, next_page, reason = await search_vector(
                client, term, kind, group, manifest, budgets, tracker, page0, run_id, seen)
            result.hits.extend(hits)
            if obs is not None:
                result.observations.append(obs)
            result.gated_filtered_out += gated_out
            if next_page != -1:
                result.status = "INCOMPLETE"
                result.incomplete_reason = reason or "budget_exhausted"
                if signing_key is not None:
                    result.continuation = mint_continuation(
                        run_id, caller_id, "stage1:" + mh, "search",
                        {"vi": vi, "group": group, "page": next_page}, signing_key)
                return result
    return result
