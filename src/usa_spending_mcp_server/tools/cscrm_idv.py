"""Tool 2: traverse_idv_hierarchy — deterministic IDV downward traversal."""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from usa_spending_mcp_server.client import USASpendingClient
from usa_spending_mcp_server.cscrm_lib import limited_get, limited_post

logger = logging.getLogger(__name__)

# Fallback keep-filter when no Stage 0 manifest is supplied (PACS / C-CURE family).
DEFAULT_KEEP_RE = re.compile(
    r"\b(pacs|iess|electronic\s+security|access\s+control|c-?cure|ccure|"
    r"c\u2022cure|c\*cure|istar|software\s+house|security\s+install|"
    r"physical\s+security)\b", re.IGNORECASE)


def product_keep_matcher(manifest: dict[str, Any] | None,
                         keep_keywords: list[str] | None):
    """Return a predicate: does this text evidence the target product scope?"""
    patterns: list[str] = []
    if manifest and manifest.get("positive_domain_context_regex"):
        patterns.append(manifest["positive_domain_context_regex"])
    lits = list(keep_keywords or []) + list((manifest or {}).get("standalone_keywords", []))
    lits = [k for k in lits if k]
    lit_res = [re.escape(k) for k in lits]
    norm_lits = [re.sub(r"[\s\-_*.•/]+", "", k).lower() for k in lits]
    if lit_res:
        patterns.append("|".join(lit_res))
    if not patterns:
        base_re = DEFAULT_KEEP_RE
    else:
        base_re = re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)

    def matches(text: str | None) -> bool:
        t = text or ""
        if base_re.search(t):
            return True
        tl = t.lower()
        if any(k.lower() in tl for k in lits):
            return True
        # Separator-normalized fallback: "C*CURE" / "C- CURE" collapse to "ccure".
        norm = re.sub(r"[\s\-_*.•/]+", "", tl)
        return any(nl and nl in norm for nl in norm_lits)

    return matches


async def _paginate_idv_children(client: USASpendingClient, parent_gid: str, kind: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        resp = await limited_post(client, "idvs/awards/",
                                  {"award_id": parent_gid, "type": kind, "limit": 100, "page": page})
        batch = resp.get("results", []) if isinstance(resp, dict) else []
        rows.extend(batch)
        meta = resp.get("page_metadata", {}) if isinstance(resp, dict) else {}
        if not meta.get("hasNext") or not batch:
            break
        page += 1
    return rows


async def _paginate_activity(client: USASpendingClient, parent_gid: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        resp = await limited_post(client, "idvs/activity/",
                                  {"award_id": parent_gid, "limit": 100, "page": page})
        batch = resp.get("results", []) if isinstance(resp, dict) else []
        rows.extend(batch)
        meta = resp.get("page_metadata", {}) if isinstance(resp, dict) else {}
        if not meta.get("hasNext") or not batch:
            break
        page += 1
    return rows


async def traverse_idv_core(
    client: USASpendingClient,
    parent_gids: list[str],
    force_vehicle_class: dict[str, str] | None = None,
    today: date | None = None,
    manifest: dict[str, Any] | None = None,
    keep_keywords: list[str] | None = None,
) -> dict[str, Any]:
    """Traverse DEDICATED IDVs downward with child-count parity and ceiling burn.

    DEDICATED gate (Fix 1 strict): parent description matches the product
    keep-filter AND <= 50 children, OR <= 50 children with >= 20% keyword
    child density. Anything else is BROAD_MULTI_PURPOSE_IDV: children are
    listed for reference but only keyword-matched GIDs are retained.
    """
    today = today or date.today()
    keep = product_keep_matcher(manifest, keep_keywords)
    vehicles = []
    for gid in parent_gids:
        try:
            vehicles.append(await _traverse_one(client, gid, force_vehicle_class, today, keep))
        except Exception as exc:  # noqa: BLE001 - per-vehicle fail-soft
            logger.warning("IDV traversal failed for %s: %s", gid, exc)
            vehicles.append({"parent_gid": gid, "classification": "UNRESOLVABLE",
                             "traversal": "skipped-error", "parity_ok": False,
                             "_error": str(exc)})
    return {"vehicles": vehicles}


def classify_container(total: int, desc_match: bool, density_known: bool,
                       density: float, distinct_recipients: int) -> tuple[str, str, str]:
    """INV4 intrinsic container homogeneity (pure; no PIID prefixes).

    IsDedicated(v) <=> (ChildCount <= 50) AND (parent-scope match OR
    >= 20% keyword density) AND (exactly one distinct awardee).
    """
    homogeneity = ("single-awardee" if distinct_recipients == 1
                   else ("multi-awardee" if distinct_recipients > 1
                         else "unverified-empty-activity"))
    gate_a = desc_match and total <= 50
    gate_b = total <= 50 and density_known and density >= 0.20
    if (gate_a or gate_b) and homogeneity == "single-awardee":
        return ("DEDICATED_SINGLE_PURPOSE_IDV",
                "gate-A-description" if gate_a else "gate-B-density", homogeneity)
    if total > 50:
        return "BROAD_MULTI_PURPOSE_IDV", "broad-exceeds-50-children", homogeneity
    if homogeneity == "multi-awardee":
        return "BROAD_MULTI_PURPOSE_IDV", "broad-multi-awardee-matoc", homogeneity
    if homogeneity == "unverified-empty-activity":
        return "BROAD_MULTI_PURPOSE_IDV", "broad-unverified-homogeneity", homogeneity
    if not desc_match:
        return "BROAD_MULTI_PURPOSE_IDV", "broad-no-parent-scope-match", homogeneity
    return "BROAD_MULTI_PURPOSE_IDV", "broad-low-keyword-density", homogeneity


async def _traverse_one(
    client: USASpendingClient,
    gid: str,
    force_vehicle_class: dict[str, str] | None,
    today: date,
    keep,
) -> dict[str, Any]:
    # NOTE: trailing slashes are mandatory — the API 301s slashless GETs.
    detail = await limited_get(client, f"awards/{gid}/")
    try:
        amounts = await limited_get(client, f"idvs/amounts/{gid}/")
    except Exception as exc:  # noqa: BLE001 - parity source is expendable
        # The amounts endpoint is parity/burn evidence only. If it is
        # unreachable while awards/ and the POST endpoints work, proceed
        # with child enumeration and leave parity unverified (None)
        # rather than nulling the whole vehicle.
        logger.warning("IDV amounts unavailable for %s: %s", gid, exc)
        amounts = {}
    piid = detail.get("piid") if isinstance(detail, dict) else None
    parent_desc = (detail.get("description") or "") if isinstance(detail, dict) else ""
    children = await _paginate_idv_children(client, gid, "child_awards")
    activity = await _paginate_activity(client, gid)
    expected = int(amounts.get("child_award_count") or 0) if amounts else None
    total = len(children)
    desc_match = bool(keep(parent_desc))
    described = [c for c in children if c.get("description")]
    matched = [c for c in described if keep(c.get("description"))]
    density = (len(matched) / total) if total else 0.0
    density_known = total > 0 and len(described) / total >= 0.5
    # INV4 intrinsic container homogeneity: a multi-award vehicle (MATOC /
    # GWAC / multi-prime IDIQ) is by definition broad, regardless of PIID
    # prefix. Recipient identity comes from activity rows (child rows carry
    # no recipient fields); recipient_id is a stable per-vendor hash.
    awardees: set[str] = set()
    for row in activity:
        rid = row.get("recipient_id") or (row.get("recipient_name") or "").strip().upper()
        if rid:
            awardees.add(str(rid))
    distinct_recipients = len(awardees)
    vehicle_class, rule, homogeneity = classify_container(
        total, desc_match, density_known, density, distinct_recipients)
    if force_vehicle_class and gid in force_vehicle_class:
        vehicle_class = force_vehicle_class[gid]
        rule = "forced"
    keyword_matched_gids = [c.get("generated_unique_award_id") for c in children
                            if c.get("generated_unique_award_id") and keep(c.get("description"))]
    entry: dict[str, Any] = {
        "parent_gid": gid, "parent_piid": piid,
        "classification": vehicle_class,
        "classification_rule": rule,
        "parent_description_match": desc_match,
        "keyword_density": round(density, 3),
        "keyword_matched_children": len(keyword_matched_gids),
        "keyword_matched_child_gids": keyword_matched_gids,
        "distinct_recipient_count": distinct_recipients,
        "homogeneity": homogeneity,
        "detail": {"total_obligation": detail.get("total_obligation"),
                   "base_and_all_options": detail.get("base_and_all_options"),
                   "period_of_performance": detail.get("period_of_performance")},
        "amounts": amounts if isinstance(amounts, dict) else {},
    }
    if vehicle_class != "DEDICATED_SINGLE_PURPOSE_IDV":
        entry["children"] = []
        entry["traversal"] = "skipped-broad-vehicle"
        if rule == "broad-unverified-homogeneity":
            # Fail-closed on money, fail-open on enumeration: keep children
            # listed (parity/canary evidence) but block Tier-2 inheritance
            # until homogeneity is verified from hydrated recipient UEIs.
            entry["children"] = children
            entry["child_count"] = len(children)
            entry["expected_child_count"] = expected
            entry["parity_ok"] = (len(children) == expected) if expected is not None else None
            entry["tier2_inheritance_blocked"] = True
            entry["traversal"] = "enumerated-homogeneity-unverified"
        return entry
    potential = detail.get("base_and_all_options") or 0
    obligated = float(amounts.get("child_award_total_obligation") or 0) if amounts else 0.0
    burn = (obligated / potential) if (amounts and potential) else None
    pop = detail.get("period_of_performance") or {}
    end_raw = (pop.get("potential_end_date") or pop.get("end_date") or "")[:10]
    days_left = None
    try:
        days_left = (date.fromisoformat(end_raw) - today).days if end_raw else None
    except ValueError:
        days_left = None
    entry.update({
        "children": children,
        "child_count": len(children),
        "expected_child_count": expected,
        "parity_ok": (len(children) == expected) if expected is not None else None,
        "activity_rows": len(activity),
        "ceiling_burn_pct": round(burn, 4) if burn is not None else None,
        "recompete_flag": bool((burn is not None and burn >= 0.75) or (days_left is not None and days_left <= 180)),
    })
    return entry


def register_traverse_idv_hierarchy(mcp: FastMCP, client: USASpendingClient) -> None:
    @mcp.tool(
        name="traverse_idv_hierarchy",
        annotations={"title": "Deterministic IDV downward traversal",
                     "readOnlyHint": True, "destructiveHint": False,
                     "idempotentHint": True, "openWorldHint": True},
    )
    async def traverse_idv_hierarchy(
        parent_gids: Annotated[list[str], Field(description="Parent IDV GIDs.")],
        force_vehicle_class: Annotated[
            dict[str, str] | None,
            Field(description="Optional GID -> class override."),
        ] = None,
        manifest: Annotated[
            dict[str, Any] | None,
            Field(description="Stage 0 SearchScopeManifest (keep-filter + keywords for the DEDICATED gate)."),
        ] = None,
        keep_keywords: Annotated[
            list[str] | None,
            Field(description="Product keywords for the keep-filter when no manifest is passed."),
        ] = None,
    ) -> dict[str, Any]:
        """Classify IDV vehicles and traverse dedicated ones to child contracts."""
        try:
            return await traverse_idv_core(client, parent_gids, force_vehicle_class,
                                           manifest=manifest, keep_keywords=keep_keywords)
        except Exception as exc:  # noqa: BLE001 - structured, fail-closed
            logger.warning("traverse_idv_hierarchy failed: %s", exc)
            return {"error": "USAspending IDV traversal failed."}
