"""C-SCRM deep-inspection tool: full award detail for a single contract/IDV."""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from usa_spending_mcp_server.client import USASpendingClient

logger = logging.getLogger(__name__)

_GENERATED_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _split_agency(raw: Any) -> dict[str, Any] | None:
    """Split a raw awarding/funding agency object into toptier vs subtier parts."""
    if not isinstance(raw, dict):
        return None
    top = raw.get("toptier_agency") or {}
    sub = raw.get("subtier_agency") or {}
    return {
        "toptier_name": top.get("name"),
        "toptier_code": top.get("code"),
        "toptier_abbreviation": top.get("abbreviation"),
        "subtier_name": sub.get("name"),
        "subtier_code": sub.get("code"),
        "subtier_abbreviation": sub.get("abbreviation"),
        "office_agency_name": raw.get("office_agency_name"),
        "agency_id": raw.get("id"),
        "has_agency_page": raw.get("has_agency_page"),
    }


def _flatten_hierarchy(raw: Any) -> list[dict[str, Any]]:
    """Flatten a psc/naics hierarchy object into an ordered level list."""
    if not isinstance(raw, dict):
        return []
    levels: list[dict[str, Any]] = []
    for level in ("toptier_code", "midtier_code", "subtier_code", "base_code"):
        node = raw.get(level)
        if isinstance(node, dict) and node.get("code"):
            levels.append(
                {
                    "level": level,
                    "code": node.get("code"),
                    "description": node.get("description"),
                }
            )
    return levels


def register_cscrm_award_details(mcp: FastMCP, client: USASpendingClient) -> None:
    @mcp.tool(
        name="get_award_cscrm_details",
        annotations={
            "title": "Deep-inspect a single award for C-SCRM attribution",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def get_award_cscrm_details(
        generated_internal_id: Annotated[
            str,
            Field(
                min_length=1,
                max_length=255,
                pattern=r"^[A-Za-z0-9_-]+$",
                description=(
                    "Award generated_internal_id from discover_agencies_by_target "
                    "representative_awards (e.g. CONT_AWD_HC102822F0982_9700_HC102817D0001_9700)."
                ),
            ),
        ],
    ) -> dict[str, Any]:
        """Deep-inspect one award for C-SCRM attribution.

        Queries the live USAspending.gov v2 API (no mocked data) via
        GET /awards/{generated_internal_id}/. Use for high-value contracts
        where spending_by_award rows come back with NULL agency/UEI fields:
        the detail record carries the full parent recipient, PSC/NAICS
        hierarchies, and split awarding vs funding agency objects.

        Returns a tiered payload with parent_recipient_name,
        parent_recipient_uei, psc_hierarchy, naics_hierarchy, and separate
        awarding_agency / funding_agency objects (toptier vs subtier split).
        """
        gid = generated_internal_id.strip()
        if not _GENERATED_ID_RE.fullmatch(gid):
            return {"error": "Invalid generated_internal_id: must match ^[A-Za-z0-9_-]+$."}

        try:
            detail = await client.get(f"awards/{gid}/")
        except Exception as exc:  # noqa: BLE001 - surfaced as structured error
            logger.warning("USAspending award detail request failed for %s: %s", gid, exc)
            return {
                "error": "USAspending award detail request failed.",
                "hint": "Verify the generated_internal_id came from a representative_awards entry.",
            }

        if not isinstance(detail, dict):
            return {"error": "Unexpected award detail response shape."}

        recipient = detail.get("recipient") or {}
        pop = detail.get("period_of_performance") or {}
        psc_raw = detail.get("psc_hierarchy") or {}
        naics_raw = detail.get("naics_hierarchy") or {}

        return {
            "generated_internal_id": detail.get("generated_unique_award_id") or gid,
            "award_id": detail.get("id"),
            "piid": detail.get("piid"),
            "type": detail.get("type"),
            "type_description": detail.get("type_description"),
            "category": detail.get("category"),
            "description": detail.get("description"),
            "date_signed": detail.get("date_signed"),
            "total_obligation": detail.get("total_obligation"),
            "base_and_all_options": detail.get("base_and_all_options"),
            "parent_recipient_name": recipient.get("parent_recipient_name"),
            "parent_recipient_uei": recipient.get("parent_recipient_uei"),
            "recipient_name": recipient.get("recipient_name"),
            "recipient_uei": recipient.get("recipient_uei"),
            "psc_hierarchy": psc_raw,
            "psc_levels": _flatten_hierarchy(psc_raw),
            "naics_hierarchy": naics_raw,
            "naics_levels": _flatten_hierarchy(naics_raw),
            "awarding_agency": _split_agency(detail.get("awarding_agency")),
            "funding_agency": _split_agency(detail.get("funding_agency")),
            "period_of_performance": {
                "start_date": pop.get("start_date"),
                "end_date": pop.get("end_date"),
                "potential_end_date": pop.get("potential_end_date"),
            },
            "parent_award": detail.get("parent_award"),
            "as_of_source": "live USAspending.gov v2 API, no mocked data",
        }
