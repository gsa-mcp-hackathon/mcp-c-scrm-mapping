"""Tool 1: search_awards_exhaustive — count-first, group-split, paginated award search."""

from __future__ import annotations

import logging
from datetime import date
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from usa_spending_mcp_server.client import USASpendingClient
from usa_spending_mcp_server.cscrm_lib import (
    CONTRACT_CODES,
    IDV_CODES,
    award_count,
    paginate_awards,
)
from usa_spending_mcp_server.cscrm_systemic import brand_token_re

logger = logging.getLogger(__name__)

AWARD_FIELDS = [
    "Award ID",
    "Recipient Name",
    "Awarding Agency",
    "Funding Agency",
    "Award Amount",
    "Description",
    "End Date",
    "Recipient UEI",
    "generated_internal_id",
]

MAX_DEPTH = 3


def _midpoint(start: str, end: str) -> str:
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    return (s + (e - s) // 2).isoformat()


async def _collect_group(
    client: USASpendingClient,
    keywords: list[str],
    codes: list[str],
    start: str,
    end: str,
    depth: int,
    expected: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collect one vehicle group, recursively halving the window past 9,500 hits."""
    if expected >= 9500 and depth < MAX_DEPTH:
        mid = _midpoint(start, end)
        rows_a, par_a = await _collect_group(client, keywords, codes, start, mid, depth + 1, expected // 2 + 1)
        rows_b, par_b = await _collect_group(client, keywords, codes, mid, end, depth + 1, expected // 2 + 1)
        return rows_a + rows_b, {"split": [start, mid, end], "left": par_a, "right": par_b}
    filters: dict[str, Any] = {
        "time_period": [{"start_date": start, "end_date": end}],
        "award_type_codes": codes,
        "keywords": keywords,
    }
    rows, parity = await paginate_awards(client, filters, AWARD_FIELDS)
    parity["expected"] = expected
    parity["parity_ok"] = (len(rows) == expected) and not parity["has_next"]
    return rows, parity


def dedupe_awards(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """INV5/REG-10: GID-primary-key deduplication. Bare-PIID collisions
    (same Award ID, different GIDs) always survive as separate records."""
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        seen.setdefault(row.get("generated_internal_id", ""), row)
    return list(seen.values())


async def search_awards_exhaustive_core(
    client: USASpendingClient,
    keywords: list[str],
    start_date: str = "2023-10-01",
    end_date: str = "2026-12-31",
    include_idvs: bool = False,
    context_gated_keywords: list[str] | None = None,
    positive_domain_context_regex: str | None = None,
    negative_drop_regex: str | None = None,
) -> dict[str, Any]:
    """Count-first, group-split, exhaustive award discovery (no NAICS/PSC filters).

    INV1 gated search: context-gated terms run as their own keyword passes,
    then each row must satisfy the mandatory positive domain co-occurrence
    regex (and no negative drop-filter) before hydration. Standalone terms
    are never post-filtered. Gated-in rows carry ``gated_match=True`` and the
    conferring rule so downstream adjudication stays evidence-typed.
    """
    import re as _re
    gated = [k for k in (context_gated_keywords or []) if k and k not in (keywords or [])]
    pos_rx = None
    if positive_domain_context_regex:
        try:
            pos_rx = _re.compile(positive_domain_context_regex, _re.IGNORECASE)
        except _re.error:
            pos_rx = None
    neg_rx = None
    if negative_drop_regex:
        try:
            neg_rx = _re.compile(negative_drop_regex, _re.IGNORECASE)
        except _re.error:
            neg_rx = None

    async def _run_terms(terms: list[str], gated_pass: bool) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
        if not terms:
            return [], {}, 0
        base_filters: dict[str, Any] = {
            "time_period": [{"start_date": start_date, "end_date": end_date}],
            "award_type_codes": CONTRACT_CODES + (IDV_CODES if include_idvs else []),
            "keywords": terms,
        }
        counts = await award_count(client, base_filters)
        total = sum(counts.values())
        contracts_n = counts.get("contracts", total)
        idvs_n = counts.get("idvs", 0 if "contracts" in counts or "idvs" in counts else total)
        groups: list[tuple[str, list[str], int]] = []
        if contracts_n > 0:
            groups.append(("contracts", CONTRACT_CODES, contracts_n))
        if include_idvs and idvs_n > 0:
            groups.append(("idvs", IDV_CODES, idvs_n))
        awards: list[dict[str, Any]] = []
        parities: dict[str, Any] = {}
        for name, codes, expected in groups:
            rows, parity = await _collect_group(client, terms, codes, start_date, end_date, 0, expected)
            awards.extend(rows)
            parities[name] = parity
        if gated_pass:
            kept = []
            for row in awards:
                text = f"{row.get('Description') or ''} {row.get('Recipient Name') or ''}"
                if neg_rx is not None and neg_rx.search(text):
                    continue
                # Guarded token match (INV2): inflectional derivatives of the
                # gated term itself do not confer co-occurrence.
                guarded = any(brand_token_re(t).search(text) for t in terms)
                m = pos_rx.search(text) if pos_rx is not None else None
                if guarded and m:
                    row = dict(row)
                    row["gated_match"] = True
                    row["gated_rule"] = f"GATED_COOCCURRENCE:{m.group(0)[:60]}"
                    kept.append(row)
            return kept, parities, len(awards) - len(kept)
        return awards, parities, 0

    awards, parities, _ = await _run_terms(list(keywords or []), False)
    g_awards, g_parities, g_filtered = await _run_terms(gated, True)
    awards.extend(g_awards)
    for k, v in g_parities.items():
        parities[f"gated:{k}"] = v
    standalone_counts = await award_count(client, {
        "time_period": [{"start_date": start_date, "end_date": end_date}],
        "award_type_codes": CONTRACT_CODES + (IDV_CODES if include_idvs else []),
        "keywords": list(keywords or []),
    }) if keywords else {}

    seen = dedupe_awards(awards)
    return {
        "keywords": keywords,
        "window": {"start_date": start_date, "end_date": end_date},
        "counts": standalone_counts,
        "groups_run": list(["contracts"] + (["idvs"] if include_idvs else [])),
        "fetched_total": len(awards),
        "unique_awards": len(seen),
        "parity": parities,
        "gated_terms": gated,
        "gated_filtered_out": g_filtered,
        "awards": seen,
    }


def register_search_awards_exhaustive(mcp: FastMCP, client: USASpendingClient) -> None:
    @mcp.tool(
        name="search_awards_exhaustive",
        annotations={"title": "Exhaustive award search with pagination parity",
                     "readOnlyHint": True, "destructiveHint": False,
                     "idempotentHint": True, "openWorldHint": True},
    )
    async def search_awards_exhaustive(
        keywords: Annotated[list[str], Field(description="Keyword terms (OR semantics).")],
        start_date: Annotated[str, Field(description="Window start (YYYY-MM-DD).")] = "2023-10-01",
        end_date: Annotated[str, Field(description="Window end (YYYY-MM-DD).")] = "2026-12-31",
        include_idvs: Annotated[bool, Field(description="Also sweep IDV umbrellas.")] = False,
        context_gated_keywords: Annotated[list[str] | None, Field(description="Gated terms: co-occurrence filtered.")] = None,
        positive_domain_context_regex: Annotated[str | None, Field(description="Mandatory co-occurrence regex for gated terms.")] = None,
        negative_drop_regex: Annotated[str | None, Field(description="Drop-filter for gated terms.")] = None,
    ) -> dict[str, Any]:
        """Exhaustive award discovery: pre-flight count, vehicle-group split, full pagination."""
        try:
            return await search_awards_exhaustive_core(client, keywords, start_date, end_date, include_idvs,
                                                      context_gated_keywords, positive_domain_context_regex,
                                                      negative_drop_regex)
        except Exception as exc:  # noqa: BLE001 - structured, fail-closed
            logger.warning("search_awards_exhaustive failed: %s", exc)
            return {"error": "USAspending exhaustive award search failed.",
                    "hint": "Retry; transient 429/503/504 are retried internally."}
