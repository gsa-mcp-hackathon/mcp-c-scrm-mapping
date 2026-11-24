"""Tool 4: fetch_award_full_context — full hydration with transaction-basis windows."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from usa_spending_mcp_server.client import USASpendingClient
from usa_spending_mcp_server.cscrm_lib import (
    CONTRACT_CODES,
    fpds_subtier_from_gid,
    hhs_component_from_gid,
    limited_get,
    limited_post,
)

logger = logging.getLogger(__name__)

TX_FIELDS = [
    "Award ID", "generated_internal_id", "Action Date", "Transaction Amount",
    "Transaction Description", "Award Type", "Action Type", "Recipient Name", "Recipient UEI",
    "Awarding Agency", "Awarding Sub Agency", "Funding Agency", "Funding Sub Agency",
    "Mod", "internal_id",
]

# FPDS termination action types: any mod carrying one makes the award historical.
TERMINATION_ACTION_TYPES = frozenset({"C1", "C2", "C3", "C4"})


def compute_window_sums(txs: list[dict[str, Any]], start_date: str,
                        end_date: str) -> tuple[float, int, bool]:
    """INV5 atomic GID accounting: sum 100% of deduplicated in-window mods
    (no text filtering). Returns (in_window_total, zero_rows_excluded,
    net_negative_flag)."""
    priced = [t for t in txs if (t.get("Transaction Amount") or 0) != 0]
    in_window = round(sum(
        t.get("Transaction Amount") or 0 for t in priced
        if start_date <= (t.get("Action Date") or "")[:10] <= end_date
    ), 2)
    return in_window, len(txs) - len(priced), in_window < 0


def classify_ghost(txs: list[dict[str, Any]], lifetime: float | None,
                   in_window: float, end_raw: str,
                   end_date: str) -> tuple[bool, str | None]:
    """INV6 ghost rule with non-empty reason enum (pure)."""
    terminated = any((t.get("Action Type") or "").upper() in TERMINATION_ACTION_TYPES
                     for t in txs)
    ghost = (terminated or (lifetime is not None and lifetime <= 0.01)
             or (in_window <= 0.01 and bool(end_raw) and end_raw < end_date))
    if not ghost:
        return False, None
    if terminated:
        return True, "TERMINATED_MOD_C1_C4"
    if lifetime is not None and lifetime <= 0.01:
        return True, "ZERO_OR_NEGATIVE_LIFETIME"
    if in_window < 0:
        return True, "IN_WINDOW_NET_DEOBLIGATION"
    return True, "ENDED_PRE_WINDOW_ZERO_IN_WINDOW"


def _toptier_code(obj: Any) -> str | None:
    return ((obj or {}).get("toptier_agency") or {}).get("code")


def _subtier_name(obj: Any) -> str | None:
    return ((obj or {}).get("subtier_agency") or {}).get("name")


async def _transactions_for_piid(
    client: USASpendingClient, piid: str, start: str, end: str
) -> list[dict[str, Any]]:
    """Fetch modification transactions via PIID-as-keyword, attributed strictly by GID."""
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        resp = await limited_post(
            client, "search/spending_by_transaction",
            {"filters": {"time_period": [{"start_date": start, "end_date": end}],
                          "award_type_codes": CONTRACT_CODES, "keywords": [piid]},
             "fields": TX_FIELDS, "sort": "Transaction Amount", "order": "desc",
             "limit": 100, "page": page},
        )
        batch = resp.get("results", []) if isinstance(resp, dict) else []
        rows.extend(batch)
        meta = resp.get("page_metadata", {}) if isinstance(resp, dict) else {}
        if not meta.get("hasNext") or not batch:
            break
        page += 1
    return rows


async def _hydrate_one(
    client: USASpendingClient,
    gid: str,
    start_date: str,
    end_date: str,
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    """Hydrate a single award (concurrency-safe; fail-soft per award)."""
    async with sem:
        try:
            # NOTE: trailing slash mandatory — the API 301s slashless GETs.
            detail = await limited_get(client, f"awards/{gid}/")
        except Exception as exc:  # noqa: BLE001 - per-award fail-soft
            logger.warning("detail lookup failed for %s: %s", gid, exc)
            return {"gid": gid, "_error": str(exc)}
        piid = detail.get("piid")
        tx_all = await _transactions_for_piid(client, piid or "", start_date, end_date)
        tx_all = [t for t in tx_all if t.get("generated_internal_id") == gid]
        seen: set[tuple] = set()
        txs: list[dict[str, Any]] = []
        for t in tx_all:
            key = (gid, t.get("Mod"), (t.get("Action Date") or "")[:10], t.get("Transaction Amount"))
            if key in seen:
                continue
            seen.add(key)
            txs.append(t)
        # Ghost-transaction filter: $0 rows are text-match artifacts, never
        # obligations. They are excluded from windowed sums (counted for the
        # record). Net-negative windows indicate de-obligation, flagged rather
        # than silently netted.
        in_window, zero_amount_rows_excluded, deobligated_flag = \
            compute_window_sums(txs, start_date, end_date)
        # Fix 3 (strict ghost rule): termination mods, zero lifetime, or
        # zero in-window on an ended award -> HISTORICAL_OR_DEOBLIGATED.
        pop_raw = detail.get("period_of_performance") or {}
        end_raw = (pop_raw.get("end_date") or "")[:10]
        lifetime = detail.get("total_obligation")
        ghost, historical_reason = classify_ghost(txs, lifetime, in_window,
                                                  end_raw, end_date)
        awarding = detail.get("awarding_agency") or {}
        funding = detail.get("funding_agency") or {}
        awarding_top = _toptier_code(awarding)
        funding_top = _toptier_code(funding)
        assisted = None
        funding_rows: list[dict[str, Any]] = []
        if awarding_top and funding_top and awarding_top != funding_top:
            try:
                fresp = await limited_post(client, "awards/funding/", {"award_id": gid})
                funding_rows = fresp.get("results", []) if isinstance(fresp, dict) else []
            except Exception as exc:  # noqa: BLE001 - funding rows are enrichment only
                logger.warning("File C funding lookup failed for %s: %s", gid, exc)
            if awarding_top == "097" and funding_top == "096":
                assisted = "INTERNAL_USACE_CIVIL_WORKS_MAPPING"
            else:
                assisted = "GENUINE_ASSISTED_ACQUISITION"
        recipient = detail.get("recipient") or {}
        pop = detail.get("period_of_performance") or {}
        psc = detail.get("psc_hierarchy") or {}
        naics = detail.get("naics_hierarchy") or {}
        return {
            "gid": gid, "piid": piid,
            "parent_gid": (detail.get("parent_award") or {}).get("generated_unique_award_id"),
            "parent_piid": (detail.get("parent_award") or {}).get("piid"),
            "recipient_name": recipient.get("recipient_name"),
            "recipient_uei": recipient.get("recipient_uei"),
            "parent_recipient_name": recipient.get("parent_recipient_name"),
            "parent_recipient_uei": recipient.get("parent_recipient_uei"),
            "awarding_toptier": awarding_top,
            "awarding_subtier": _subtier_name(awarding),
            "funding_toptier": funding_top,
            "funding_subtier": _subtier_name(funding),
            "fpds_subtier_code": fpds_subtier_from_gid(gid),
            "hhs_component": hhs_component_from_gid(gid),
            "naics_code": (naics.get("base_code") or {}).get("code"),
            "naics_description": (naics.get("base_code") or {}).get("description"),
            "psc_code": (psc.get("base_code") or {}).get("code"),
            "base_description": detail.get("description"),
            "start_date": pop.get("start_date"),
            "end_date": pop.get("end_date"),
            "lifetime_obligated": detail.get("total_obligation"),
            "potential_ceiling": detail.get("base_and_all_options"),
            "in_window_obligated": in_window,
            "zero_amount_rows_excluded": zero_amount_rows_excluded,
            "deobligated_flag": deobligated_flag,
            "ghost_historical": ghost,
            "historical_reason": historical_reason,
            "transactions": txs,
            "assisted_classification": assisted,
            "funding_rows": funding_rows,
        }


async def fetch_award_full_context_core(
    client: USASpendingClient,
    gids: list[str],
    start_date: str = "2023-10-01",
    end_date: str = "2026-12-31",
    max_concurrency: int = 4,
) -> dict[str, Any]:
    """Hydrate awards concurrently (semaphore-gated), fail-soft per award."""
    sem = asyncio.Semaphore(max_concurrency)
    hydrated = await asyncio.gather(
        *[_hydrate_one(client, gid, start_date, end_date, sem) for gid in gids]
    )
    return {"awards": list(hydrated)}


def register_fetch_award_full_context(mcp: FastMCP, client: USASpendingClient) -> None:
    @mcp.tool(
        name="fetch_award_full_context",
        annotations={"title": "Full award hydration with windowed accounting",
                     "readOnlyHint": True, "destructiveHint": False,
                     "idempotentHint": True, "openWorldHint": True},
    )
    async def fetch_award_full_context(
        gids: Annotated[list[str], Field(description="Award GIDs to hydrate.")],
        start_date: Annotated[str, Field(description="Window start.")] = "2023-10-01",
        end_date: Annotated[str, Field(description="Window end.")] = "2026-12-31",
    ) -> dict[str, Any]:
        """Return fully hydrated award contexts with transaction-basis in-window sums."""
        try:
            return await fetch_award_full_context_core(client, gids, start_date, end_date)
        except Exception as exc:  # noqa: BLE001 - structured, fail-closed
            logger.warning("fetch_award_full_context failed: %s", exc)
            return {"error": "Award hydration failed."}
