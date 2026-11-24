"""Stage 3 — IDV hierarchy traversal (global-count-verified dedication).

Dedication requires GLOBAL evidence, never the local subset alone:
  IsDedicated(v) <=> global ChildCount in [2, 50] AND (parent-scope match OR
  >= 20% keyword density) AND exactly one distinct awardee AND no verified
  broad vehicle-type identity.

Verified vehicle identity comes only from live type fields on the parent
award detail (`type`/`type_description`) or the idvs amounts record — never
from identifier-prefix lists. A broad/multi-agency/GWAC/MAC/MATOC type
forces BROAD; a global count of 1 yields SINGLETON_OBSERVED (child recorded,
no dedication inference); an unavailable global count yields
UNKNOWN_DEDICATION (never DEDICATED).

Endpoints: GET awards/{gid}/, GET idvs/amounts/{gid}/ (global count/parity/
obligation source, fail-soft), POST idvs/awards/ {award_id, type:
child_awards, limit 100}, POST idvs/activity/ {award_id, limit 100} —
trailing slashes mandatory. Immediate children only (depth 1); BROAD,
singleton, and unknown-dedication vehicles are enumerated, never hydrated
in bulk. Keyword-confirmed children are retained as evidence.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from usaspending_cscrm_mcp.client import USAspendingClient
from usaspending_cscrm_mcp.execution import BudgetTracker, RunBudgets
from usaspending_cscrm_mcp.models import RelationshipEdge

logger = logging.getLogger(__name__)


class VehicleResult(BaseModel):
    parent_gid: str
    parent_piid: str = ""
    classification: str = "BROAD_MULTI_PURPOSE_IDV"
    classification_rule: str = ""
    parent_description_match: bool = False
    keyword_density: float = 0.0
    keyword_matched_children: int = 0
    keyword_matched_child_gids: list[str] = Field(default_factory=list)
    distinct_recipient_count: int = 0
    homogeneity: str = ""
    child_count: int = 0
    expected_child_count: int | None = None
    global_child_count: int | None = None
    vehicle_type_source: str = "local"
    vehicle_identity: str = "UNKNOWN"
    expired_parent: bool = False
    parity_ok: bool | None = None
    ceiling_burn: float | None = None
    days_remaining: int | None = None
    traversal: str = ""
    tier2_inheritance_blocked: bool = False
    edges: list[RelationshipEdge] = Field(default_factory=list)


_TYPE_TEXT_KEYS = ("type", "type_description", "contract_award_type", "vehicle_type")

_BROAD_TYPE_RE = re.compile(r"\bGWAC\b|\bMAC\b|\bMATOC\b|\bMULTI\s*AGENCY\b")

_LEGACY_LOCAL_SUBSET: Any = object()


def _type_texts(record: Any) -> list[str]:
    """Non-empty vehicle/contract type strings from one live record."""
    if not isinstance(record, dict):
        return []
    texts = []
    for key in _TYPE_TEXT_KEYS:
        val = record.get(key)
        if isinstance(val, str) and val.strip():
            texts.append(val)
    return texts


def _broad_vehicle_type(texts: list[str]) -> bool:
    """True when live type fields assert a broad/multi-agency identity."""
    blob = re.sub(r"[-_/]+", " ", " ".join(texts).upper())
    return bool(_BROAD_TYPE_RE.search(blob))


def classify_container(total: int, desc_match: bool, density_known: bool,
                       density: float, distinct_recipients: int,
                       global_child_count: Any = _LEGACY_LOCAL_SUBSET,
                       broad_vehicle_type: bool = False,
                       ) -> tuple[str, str, str]:
    """Intrinsic container homogeneity with global-count verification.

    `global_child_count` is the verified GLOBAL count from idvs/amounts
    (None = unavailable, never DEDICATED). When omitted (legacy sentinel)
    the local subset `total` is used, preserving direct-call compatibility.
    `broad_vehicle_type` reflects verified live type fields and dominates
    count heuristics. No identifier-prefix matching anywhere.
    """
    homogeneity = ("single-awardee" if distinct_recipients == 1
                   else ("multi-awardee" if distinct_recipients > 1
                         else "unverified-empty-activity"))
    if broad_vehicle_type:
        return ("BROAD_MULTI_PURPOSE_IDV",
                "broad-verified-vehicle-type", homogeneity)
    if global_child_count is _LEGACY_LOCAL_SUBSET:
        n = total
        unknown = False
    else:
        n = global_child_count
        unknown = global_child_count is None
    if unknown or n is None:
        return ("UNKNOWN_DEDICATION",
                "unknown-dedication-no-global-count", homogeneity)
    if n == 1:
        return ("SINGLETON_OBSERVED",
                "singleton-single-observed-child", homogeneity)
    gate_a = desc_match and 2 <= n <= 50
    gate_b = 2 <= n <= 50 and density_known and density >= 0.20
    if (gate_a or gate_b) and homogeneity == "single-awardee":
        return ("DEDICATED_SINGLE_PURPOSE_IDV",
                "gate-A-description" if gate_a else "gate-B-density", homogeneity)
    if n > 50:
        return "BROAD_MULTI_PURPOSE_IDV", "broad-exceeds-50-children", homogeneity
    if homogeneity == "multi-awardee":
        return "BROAD_MULTI_PURPOSE_IDV", "broad-multi-awardee-matoc", homogeneity
    if homogeneity == "unverified-empty-activity":
        return "BROAD_MULTI_PURPOSE_IDV", "broad-unverified-homogeneity", homogeneity
    if not desc_match:
        return "BROAD_MULTI_PURPOSE_IDV", "broad-no-parent-scope-match", homogeneity
    return "BROAD_MULTI_PURPOSE_IDV", "broad-low-keyword-density", homogeneity


async def _paginate(client: USAspendingClient, endpoint: str,
                    payload: dict[str, Any], tracker: BudgetTracker
                    ) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        if tracker.exhausted():
            return rows, False
        tracker.pages += 1
        body = dict(payload, page=page)
        resp = await client.post(endpoint, body)
        batch = resp.get("results", []) or []
        rows.extend([r for r in batch if isinstance(r, dict)])
        meta = resp.get("page_metadata", {}) or {}
        if not meta.get("hasNext") or not batch:
            return rows, True
        page += 1


async def traverse_vehicle(client: USAspendingClient, gid: str,
                           keep: Callable[[str | None], Any],
                           force_class: str | None = None,
                           today: date | None = None,
                           budgets: RunBudgets | None = None,
                           tracker: BudgetTracker | None = None,
                           ) -> VehicleResult | None:
    """Traverse one IDV parent. Returns None only when the parent detail
    itself is unreachable (fault isolation: siblings still traverse)."""
    budgets = budgets or RunBudgets()
    tracker = tracker or BudgetTracker(budgets)
    today = today or date.today()
    try:
        detail = await client.get(f"awards/{gid}/")
    except Exception as exc:  # noqa: BLE001 - fault isolation per vehicle
        logger.warning("IDV parent detail failed for %s: %s", gid, exc)
        return None
    if not isinstance(detail, dict):
        return None
    try:
        amounts = await client.get(f"idvs/amounts/{gid}/")
    except Exception as exc:  # noqa: BLE001 - parity source expendable
        logger.warning("IDV amounts unavailable for %s: %s", gid, exc)
        amounts = {}
    if not isinstance(amounts, dict):
        amounts = {}
    piid = detail.get("piid") or ""
    parent_desc = detail.get("description") or ""
    raw_global = amounts.get("child_award_count")
    try:
        global_count = int(raw_global) if raw_global is not None else None
    except (TypeError, ValueError):
        global_count = None
    detail_types = _type_texts(detail)
    amounts_types = _type_texts(amounts)
    broad_type = _broad_vehicle_type(detail_types + amounts_types)
    type_source = "local" if detail_types else ("global" if amounts_types else "local")
    pop = detail.get("period_of_performance")
    if not isinstance(pop, dict):
        pop = {}
    end_raw = pop.get("potential_end_date") or pop.get("end_date") or ""
    end_raw = end_raw[:10] if isinstance(end_raw, str) else ""
    try:
        end_day = date.fromisoformat(end_raw) if end_raw else None
    except ValueError:
        end_day = None
    expired = end_day is not None and end_day < today
    try:
        children, _ = await _paginate(client, "idvs/awards/",
                                      {"award_id": gid, "type": "child_awards",
                                       "limit": 100}, tracker)
        activity, _ = await _paginate(client, "idvs/activity/",
                                      {"award_id": gid, "limit": 100}, tracker)
    except Exception as exc:  # noqa: BLE001 - enumeration failure recorded
        logger.warning("IDV enumeration failed for %s: %s", gid, exc)
        return VehicleResult(parent_gid=gid, parent_piid=piid,
                             global_child_count=global_count,
                             vehicle_type_source=type_source,
                             expired_parent=expired,
                             classification_rule=f"enumeration-failed:{type(exc).__name__}",
                             traversal="failed")
    total = len(children)
    desc_match = bool(keep(parent_desc))
    described = [c for c in children if c.get("description")]
    matched = [c for c in described if keep(c.get("description"))]
    density = (len(matched) / total) if total else 0.0
    density_known = total > 0 and len(described) / total >= 0.5
    awardees: set[str] = set()
    for row in activity:
        rid = row.get("recipient_id") or (row.get("recipient_name") or "").strip().upper()
        if rid:
            awardees.add(str(rid))
    vehicle_class, rule, homogeneity = classify_container(
        total, desc_match, density_known, density, len(awardees),
        global_child_count=global_count, broad_vehicle_type=broad_type)
    if force_class:
        vehicle_class, rule = force_class, "forced"
    if broad_type:
        identity = "BROAD_VERIFIED"
    elif (vehicle_class == "DEDICATED_SINGLE_PURPOSE_IDV"
          and (detail_types or amounts_types)):
        identity = "DEDICATED_VERIFIED"
    else:
        identity = "UNKNOWN"
    matched_gids = [c.get("generated_unique_award_id") for c in children
                    if c.get("generated_unique_award_id") and keep(c.get("description"))]
    res = VehicleResult(
        parent_gid=gid, parent_piid=piid, classification=vehicle_class,
        classification_rule=rule, parent_description_match=desc_match,
        keyword_density=round(density, 3),
        keyword_matched_children=len(matched_gids),
        keyword_matched_child_gids=[g for g in matched_gids if isinstance(g, str)],
        distinct_recipient_count=len(awardees), homogeneity=homogeneity,
        child_count=total, expected_child_count=global_count,
        global_child_count=global_count, vehicle_type_source=type_source,
        vehicle_identity=identity, expired_parent=expired,
        edges=[RelationshipEdge(parent_gid=gid, child_gid=c.get("generated_unique_award_id", ""))
               for c in children if c.get("generated_unique_award_id")],
    )
    if vehicle_class == "DEDICATED_SINGLE_PURPOSE_IDV":
        res.ceiling_burn = _burn(detail, amounts)
        res.days_remaining = (end_day - today).days if end_day else None
        res.traversal = "dedicated-children-enumerated"
        res.parity_ok = (total == global_count) if global_count is not None else None
        return res
    if vehicle_class == "SINGLETON_OBSERVED":
        # Single observed child recorded exactly as enumerated (parity/burn
        # evidence retained) but with no dedication inference: days_remaining
        # stays None so no watch/embargo predicate can fire on it.
        res.ceiling_burn = _burn(detail, amounts)
        res.parity_ok = (total == global_count) if global_count is not None else None
        res.traversal = "singleton-observed-no-inference"
        return res
    if vehicle_class == "UNKNOWN_DEDICATION":
        res.parity_ok = (total == global_count) if global_count is not None else None
        res.tier2_inheritance_blocked = True
        res.traversal = "skipped-unknown-dedication"
        return res
    res.traversal = "skipped-broad-vehicle"
    if rule == "broad-unverified-homogeneity":
        res.parity_ok = (total == global_count) if global_count is not None else None
        res.tier2_inheritance_blocked = True
        res.traversal = "enumerated-homogeneity-unverified"
    return res


def _burn(detail: dict[str, Any], amounts: dict[str, Any]) -> float | None:
    """Ceiling burn = global child obligations / base_and_all_options."""
    try:
        potential = float(detail.get("base_and_all_options") or 0)
    except (TypeError, ValueError):
        potential = 0.0
    try:
        obligated = float(amounts.get("child_award_total_obligation") or 0)
    except (TypeError, ValueError):
        obligated = 0.0
    return (obligated / potential) if (amounts and potential) else None
