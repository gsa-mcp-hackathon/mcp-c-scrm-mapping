"""Shared C-SCRM helpers: lexicon, adjudication regexes, rate-limited calls,
exhaustive pagination, vehicle groups, and FPDS subtier parsing."""

from __future__ import annotations

import logging
import re
from typing import Any

from usa_spending_mcp_server.client import USASpendingClient
from usa_spending_mcp_server.rate_limit import global_bucket, with_backoff

logger = logging.getLogger(__name__)

# Default expanded discovery lexicon (C-CURE 9000 family).
DEFAULT_LEXICON = [
    "C-CURE 9000",
    "CCURE 9000",
    "C-CURE",
    "CCURE",
    "C\u2022CURE",
    "CC9000",
    "iSTAR G2",
    "iSTAR Ultra",
    "Software House",
]

CONTRACT_CODES = ["A", "B", "C", "D"]
IDV_CODES = ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E"]

# ---- Tool 5 regexes (multi-field contextual adjudication) ----
DROP_AUTOCLAVE_RE = re.compile(
    r"\b\d+\s*\u00b0?\s*[cf]\s+cure\b|\bcure\s+temp\b|\bconcrete\s+curing\b|\bepoxy\s+cure\b",
    re.I,
)
DROP_FOOD_RE = re.compile(r"\bcured\b|\bbacon\b|\bham\b|\bsausage\b|\bpork\b", re.I)
DROP_CORP_RE = re.compile(
    r"\bsoftware\s*house\s+(international|ltd|limited)\b|\bistar\s+financial\b", re.I
)
DROP_ISR_RE = re.compile(
    r"\b(intelligence[,\s]+surveillance|target\s+acquisition|reconnaissance|electronic\s+warfare|airborne\s+istar)\b",
    re.I,
)
KEEP_DIRECT_RE = re.compile(
    r"\bc[\s\-\u2022.*]*cure(\s*9000|\s*800)?\b|\bcc\s*9000\b|\bistar\s+(ultra|edge|pro|g2)\b",
    re.I,
)
KEEP_POLY_TOKEN_RE = re.compile(r"\bistar\b|\bsoftware\s*house\b", re.I)
SECURITY_CONTEXT_RE = re.compile(
    r"\b(pacs|access\s+control|card\s+reader|panel|controller|door|badge|alarm|security|mdi|tyco|johnson\s+controls|c-cure|ccure)\b",
    re.I,
)
TIER_1B_OEM_RE = re.compile(
    r"\b(mdi|monitor\s+dynamics|lenel|onguard|genetec|amag|symmetry|hirsch|identiv)\b", re.I
)
ANOMALY_NAICS_ALLOW = {"561621", "541512", "541519", "334290", "238210", "561210", "334511"}

# HHS 4-digit FPDS subtier mapping (never trust the subtier_agency name filter).
HHS_SUBTIER_MAP = {"7524": "FDA", "7523": "CDC", "7529": "NIH"}

# Broad multi-agency vehicle PIID prefixes (deterministic classification gate).
BROAD_VEHICLE_PREFIXES = ("GS", "NNG", "SPRM", "SPE", "47Q")


def classify_vehicle(piid: str | None) -> str:
    """DEDICATED_SINGLE_PURPOSE_IDV vs BROAD_MULTI_AGENCY_GWAC_MAS by PIID prefix."""
    if piid and piid.upper().startswith(BROAD_VEHICLE_PREFIXES):
        return "BROAD_MULTI_AGENCY_GWAC_MAS"
    return "DEDICATED_SINGLE_PURPOSE_IDV"


def fpds_subtier_from_gid(gid: str) -> str | None:
    """Parse the 4-digit FPDS subtier code from a generated_internal_id."""
    parts = (gid or "").split("_")
    if len(parts) >= 4 and re.fullmatch(r"\d{4}", parts[3] or ""):
        return parts[3]
    return None


def hhs_component_from_gid(gid: str) -> str | None:
    """Map an HHS award GID to FDA/CDC/NIH via its FPDS subtier segment."""
    return HHS_SUBTIER_MAP.get(fpds_subtier_from_gid(gid) or "")


async def limited_post(client: USASpendingClient, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Rate-limited POST with exponential backoff on 429/503/504."""
    await global_bucket.acquire()
    return await with_backoff(lambda: client.post(endpoint, payload))


async def limited_get(client: USASpendingClient, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Rate-limited GET with exponential backoff on 429/503/504."""
    await global_bucket.acquire()
    return await with_backoff(lambda: client.get(endpoint, params=params))


async def award_count(client: USASpendingClient, filters: dict[str, Any]) -> dict[str, int]:
    """Pre-flight counts per vehicle group via spending_by_award_count."""
    resp = await limited_post(client, "search/spending_by_award_count", {"filters": filters})
    results = resp.get("results", {}) if isinstance(resp, dict) else {}
    return {k: int(v or 0) for k, v in results.items()} if isinstance(results, dict) else {}


async def paginate_awards(
    client: USASpendingClient,
    filters: dict[str, Any],
    fields: list[str],
    *,
    limit: int = 100,
    max_pages: int = 120,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Exhaustively page spending_by_award until hasNext is false.

    Returns (rows, parity) where parity records page count and terminal
    hasNext. Guards the ~10k-record window by stopping at max_pages.
    """
    rows: list[dict[str, Any]] = []
    page = 1
    last_has_next = True
    while page <= max_pages:
        payload = {
            "filters": filters,
            "fields": fields,
            "sort": "Award Amount",
            "order": "desc",
            "limit": limit,
            "page": page,
        }
        resp = await limited_post(client, "search/spending_by_award/", payload)
        batch = resp.get("results", []) if isinstance(resp, dict) else []
        rows.extend(batch)
        meta = resp.get("page_metadata", {}) if isinstance(resp, dict) else {}
        last_has_next = bool(meta.get("hasNext"))
        if not last_has_next or not batch:
            break
        page += 1
    return rows, {"pages": page, "has_next": last_has_next, "truncated_at_cap": page >= max_pages}
