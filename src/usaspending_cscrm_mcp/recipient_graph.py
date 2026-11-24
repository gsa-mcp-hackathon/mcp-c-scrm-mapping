"""Recipient graph (structural UEI-graph classification, no default lexicon).

Contract: GET recipient/children/{uei}/ (trailing slash mandatory; list).
Channel rule (structural port, caller-supplied identity only):
- recipient/parent UEI in verified set -> OEM_DIRECT_SUBSIDIARY;
- UEI in homograph set, or name match on caller-flagged/high-collision tokens
  only -> THIRD_PARTY_INTEGRATOR;
- otherwise THIRD_PARTY_INTEGRATOR with reason (never OEM by default).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from usaspending_cscrm_mcp.client import USAspendingClient

logger = logging.getLogger(__name__)


class RecipientNode(BaseModel):
    uei: str
    children: list[dict] = Field(default_factory=list)
    channel: str = "THIRD_PARTY_INTEGRATOR"
    channel_reason: str = ""
    error: str | None = None


def is_structural_collision_token(token: str, caller_collision_terms: set[str]) -> bool:
    """Short/acronym tokens and caller-flagged collision terms — never standalone OEM evidence."""
    t = (token or "").strip().lower()
    words = t.split()
    return (not t or len(t) <= 4 or t in caller_collision_terms
            or (len(words) >= 2 and any(len(w) <= 3 for w in words)))


def classify_channel(recipient_uei: str | None, parent_uei: str | None,
                     recipient_name: str | None,
                     verified_ueis: set[str], homograph_ueis: set[str],
                     oem_name_tokens: tuple[str, ...],
                     oem_name: str | None,
                     caller_collision_terms: set[str]) -> tuple[str, str]:
    if (recipient_uei or "") in verified_ueis or (parent_uei or "") in verified_ueis:
        return "OEM_DIRECT_SUBSIDIARY", "uei-graph-membership"
    if (recipient_uei or "") in homograph_ueis:
        return "THIRD_PARTY_INTEGRATOR", "homograph-uei-excluded"
    if oem_name and is_structural_collision_token(oem_name, caller_collision_terms):
        return "THIRD_PARTY_INTEGRATOR", "oem-name-high-collision"
    hay = (recipient_name or "").lower()
    if oem_name_tokens and any(tok.lower() in hay for tok in oem_name_tokens):
        if oem_name and not is_structural_collision_token(oem_name, caller_collision_terms):
            return "OEM_DIRECT_SUBSIDIARY", "explicit-name-token-non-collision"
        return "THIRD_PARTY_INTEGRATOR", "name-token-but-collision-unresolved"
    return "THIRD_PARTY_INTEGRATOR", "no-uei-or-name-evidence"


async def resolve_trees(client: USAspendingClient, ueis: list[str], *,
                        verified_ueis: set[str], homograph_ueis: set[str],
                        oem_name_tokens: tuple[str, ...] = (),
                        oem_name: str | None = None,
                        caller_collision_terms: set[str] | None = None,
                        ) -> list[RecipientNode]:
    collision = caller_collision_terms or set()
    nodes: list[RecipientNode] = []
    for uei in ueis:
        try:
            raw = await client.get(f"recipient/children/{uei}/")
            if isinstance(raw, dict):
                records = raw.get("results", []) or []
            else:
                records = raw if isinstance(raw, list) else []
            error = None
        except Exception as exc:  # noqa: BLE001 - per-UEI fail-soft
            logger.warning("recipient children lookup failed for %s: %s", uei, exc)
            records, error = [{"_error": str(exc)}], str(exc)[:200]
        channel, reason = classify_channel(uei, None, None, verified_ueis,
                                           homograph_ueis, oem_name_tokens,
                                           oem_name, collision)
        nodes.append(RecipientNode(uei=uei, children=records, channel=channel,
                                   channel_reason=reason, error=error))
    return nodes
