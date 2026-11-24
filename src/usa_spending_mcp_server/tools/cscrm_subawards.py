"""Tool 6: search_subawards_exhaustive — segregated Tier-2 FSRS subaward ledger."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from usa_spending_mcp_server.client import USASpendingClient
from usa_spending_mcp_server.cscrm_lib import limited_post
from usa_spending_mcp_server.tools.cscrm_lexicon import adjudicate_candidate

logger = logging.getLogger(__name__)


async def _paginate_subawards(client: USASpendingClient, prime_gid: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        resp = await limited_post(client, "subawards/",
                                  {"award_id": prime_gid, "limit": 100, "page": page})
        batch = resp.get("results", []) if isinstance(resp, dict) else []
        rows.extend(batch)
        meta = resp.get("page_metadata", {}) if isinstance(resp, dict) else {}
        if not meta.get("hasNext") or not batch:
            break
        page += 1
    return rows


def adjudicate_sweep_hits(keyword_hits: list[dict[str, Any]],
                          keywords: list[str] | None,
                          manifest: dict[str, Any] | None) -> list[dict[str, Any]]:
    """INV6 universal adjudication pipe for the subaward keyword sweep."""
    adjudicated: list[dict[str, Any]] = []
    for s in keyword_hits:
        if not isinstance(s, dict):
            continue
        desc = s.get("Sub-Award Description") or ""
        verdict = adjudicate_candidate(desc, [], None,
                                       manifest=manifest, product_terms=keywords)
        if not verdict["tier"].startswith("DROPPED"):
            adjudicated.append({"sub_award_id": s.get("Sub-Award ID"),
                                "sub_award_description": (desc or "")[:240],
                                "sub_award_amount": s.get("Sub-Award Amount"),
                                "prime_award_id": s.get("Prime Award ID"),
                                "prime_gid": s.get("prime_award_generated_internal_id"),
                                "tier": verdict["tier"], "rule": verdict["rule"],
                                "evidence": verdict.get("evidence"),
                                "competing_oems": verdict.get("competing_oems", [])})
    adjudicated.sort(key=lambda r: -(r.get("sub_award_amount") or 0))
    return adjudicated


async def search_subawards_exhaustive_core(
    client: USASpendingClient,
    prime_gids: list[str],
    keywords: list[str] | None = None,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a segregated Tier-2 ledger (never merged into prime totals).

    INV6 universal adjudication pipe: every keyword-sweep subaward passes
    through adjudicate_lexical_matches; kept (non-dropped) hits render in
    Section 3 with subawardee, amount, prime linkage, and matched rule.
    """
    ledger = []
    for gid in prime_gids:
        try:
            subs = await _paginate_subawards(client, gid)
        except Exception as exc:  # noqa: BLE001 - per-prime fail-soft
            logger.warning("subaward lookup failed for %s: %s", gid, exc)
            subs = [{"_error": str(exc)}]
        ledger.append({"prime_gid": gid, "subaward_count": len(subs), "subawards": subs})
    keyword_hits: list[dict[str, Any]] = []
    sweep_warning: str | None = None
    if keywords:
        try:
            page = 1
            while True:
                resp = await limited_post(
                    client, "search/spending_by_award/",
                    {"filters": {"keywords": keywords,
                                 # required even under subawards:true (422 without it)
                                 "award_type_codes": ["A", "B", "C", "D"]},
                     "subawards": True,
                     "fields": ["Sub-Award ID", "Sub-Award Description",
                                "Sub-Award Amount", "Prime Award ID",
                                "prime_award_generated_internal_id"],
                     "sort": "Sub-Award Amount", "order": "desc",
                     "limit": 100, "page": page})
                batch = resp.get("results", []) if isinstance(resp, dict) else []
                keyword_hits.extend(batch)
                meta = resp.get("page_metadata", {}) if isinstance(resp, dict) else {}
                if not meta.get("hasNext") or not batch:
                    break
                page += 1
        except Exception as exc:  # noqa: BLE001 - sweep is advisory; ledger stands
            logger.warning("subaward keyword sweep failed: %s", exc)
            sweep_warning = f"keyword sweep unavailable: {exc}"
    result: dict[str, Any] = {"tier2_subaward_ledger": ledger,
                              "keyword_subaward_hits": keyword_hits}
    if sweep_warning:
        result["sweep_warning"] = sweep_warning
    # INV6: adjudicate every sweep hit; kept rows render in Section 3.
    adjudicated = adjudicate_sweep_hits(keyword_hits, keywords, manifest)
    result["adjudicated_subaward_hits"] = adjudicated
    result["adjudicated_subaward_kept"] = len(adjudicated)
    return result


def register_search_subawards_exhaustive(mcp: FastMCP, client: USASpendingClient) -> None:
    @mcp.tool(
        name="search_subawards_exhaustive",
        annotations={"title": "Segregated Tier-2 subaward ledger",
                     "readOnlyHint": True, "destructiveHint": False,
                     "idempotentHint": True, "openWorldHint": True},
    )
    async def search_subawards_exhaustive(
        prime_gids: Annotated[list[str] | None, Field(description="Prime award GIDs.")] = None,
        keywords: Annotated[list[str] | None, Field(description="Optional lexicon sweep.")] = None,
        manifest: Annotated[dict[str, Any] | None, Field(description="Stage 0 manifest for sweep adjudication.")] = None,
    ) -> dict[str, Any]:
        """Return FSRS subawards segregated from prime obligation totals."""
        try:
            return await search_subawards_exhaustive_core(client, prime_gids or [], keywords,
                                                         manifest=manifest)
        except Exception as exc:  # noqa: BLE001 - structured, fail-closed
            logger.warning("search_subawards_exhaustive failed: %s", exc)
            return {"error": "Subaward search failed."}
