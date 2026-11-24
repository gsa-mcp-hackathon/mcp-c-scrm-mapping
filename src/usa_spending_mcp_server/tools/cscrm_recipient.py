"""Tool 3: resolve_recipient_tree — OEM corporate family resolution."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from usa_spending_mcp_server.client import USASpendingClient
from usa_spending_mcp_server.cscrm_lib import limited_get
from usa_spending_mcp_server.cscrm_systemic import is_high_collision_token

logger = logging.getLogger(__name__)

DEFAULT_OEM_UEIS = {
    "NX52EXRHRDK7",  # Microsoft Corporation (parent)
    "CE8MGXAS9KJ6",  # Johnson Controls, Inc
    "K9HJHLAKXE96",  # Johnson Controls Fire Protection LP
}
DEFAULT_OEM_TOKENS = (
    "johnson controls",
    "johnson controls security solutions",
    "johnson controls building automation",
    "johnson controls fire protection",
    "tyco",
    "software house",
    "microsoft",
)


def classify_fulfillment_channel(
    recipient_uei: str | None,
    parent_uei: str | None,
    recipient_name: str | None,
    oem_ueis: set[str] | None = None,
    oem_tokens: tuple[str, ...] = DEFAULT_OEM_TOKENS,
    verified_oem_ueis: set[str] | None = None,
    homograph_ueis: set[str] | None = None,
    oem_name: str | None = None,
) -> str:
    """OEM_DIRECT_SUBSIDIARY vs THIRD_PARTY_INTEGRATOR (deterministic).

    INV3 UEI-graph closed world: when a verified OEM UEI family is supplied
    (Stage 0 graph), membership alone decides — substring name matching is
    prohibited whenever homograph UEIs are known or the OEM name itself is
    a high-collision token. Axis 1 answers only *who sold it*; product
    scope (Axis 2) is decided by lexical/vehicle evidence, never here.
    """
    oem_ueis = oem_ueis or DEFAULT_OEM_UEIS
    homograph_ueis = homograph_ueis or set()
    if (recipient_uei or "") in homograph_ueis:
        return "THIRD_PARTY_INTEGRATOR"
    if verified_oem_ueis is not None:
        if (recipient_uei or "") in verified_oem_ueis or (parent_uei or "") in verified_oem_ueis:
            return "OEM_DIRECT_SUBSIDIARY"
        name_collision = bool(oem_name and is_high_collision_token(oem_name))
        if homograph_ueis or name_collision:
            return "THIRD_PARTY_INTEGRATOR"
    if (recipient_uei or "") in oem_ueis or (parent_uei or "") in oem_ueis:
        return "OEM_DIRECT_SUBSIDIARY"
    hay = f"{recipient_name or ''}".lower()
    if any(tok in hay for tok in oem_tokens):
        return "OEM_DIRECT_SUBSIDIARY"
    return "THIRD_PARTY_INTEGRATOR"


async def resolve_recipient_tree_core(
    client: USASpendingClient,
    ueis: list[str],
    oem_ueis: list[str] | None = None,
    oem_tokens: list[str] | None = None,
) -> dict[str, Any]:
    """Fetch recipient children trees and classify each UEI's channel."""
    oem_uei_set = set(oem_ueis or DEFAULT_OEM_UEIS)
    tokens = tuple(oem_tokens or list(DEFAULT_OEM_TOKENS))
    trees = []
    for uei in ueis:
        try:
            # NOTE: trailing slash mandatory — the API 301s slashless GETs.
            children = await limited_get(client, f"recipient/children/{uei}/")
            child_records = children if isinstance(children, list) else []
        except Exception as exc:  # noqa: BLE001 - per-UEI fail-soft
            logger.warning("recipient children lookup failed for %s: %s", uei, exc)
            child_records = [{"_error": str(exc)}]
        trees.append({"uei": uei, "children": child_records,
                      "channel": classify_fulfillment_channel(uei, None, None, oem_uei_set, tokens)})
    return {"trees": trees}


def register_resolve_recipient_tree(mcp: FastMCP, client: USASpendingClient) -> None:
    @mcp.tool(
        name="resolve_recipient_tree",
        annotations={"title": "OEM corporate family resolution",
                     "readOnlyHint": True, "destructiveHint": False,
                     "idempotentHint": True, "openWorldHint": True},
    )
    async def resolve_recipient_tree(
        ueis: Annotated[list[str], Field(description="Recipient UEIs to resolve.")],
        oem_ueis: Annotated[list[str] | None, Field(description="OEM family UEIs.")] = None,
        oem_tokens: Annotated[list[str] | None, Field(description="OEM name tokens.")] = None,
    ) -> dict[str, Any]:
        """Enumerate recipient trees and classify OEM-direct vs integrator channels."""
        try:
            return await resolve_recipient_tree_core(client, ueis, oem_ueis, oem_tokens)
        except Exception as exc:  # noqa: BLE001 - structured, fail-closed
            logger.warning("resolve_recipient_tree failed: %s", exc)
            return {"error": "Recipient tree resolution failed."}
