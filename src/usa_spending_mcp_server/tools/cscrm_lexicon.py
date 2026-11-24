"""Tool 5: adjudicate_lexical_matches — multi-field contextual regex adjudication."""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from usa_spending_mcp_server.client import USASpendingClient
from usa_spending_mcp_server.cscrm_lib import (
    DROP_AUTOCLAVE_RE,
    DROP_CORP_RE,
    DROP_FOOD_RE,
    DROP_ISR_RE,
    KEEP_DIRECT_RE,
    KEEP_POLY_TOKEN_RE,
    SECURITY_CONTEXT_RE,
    TIER_1B_OEM_RE,
)
from usa_spending_mcp_server.cscrm_systemic import (
    DROP_HOMOGRAPH,
    DROP_VETO,
    TIER_1A,
    TIER_1B,
    TIER_1C,
    brand_token_re,
    mask_boilerplate,
    software_delivery_present,
    taxonomy_vetoed,
)

logger = logging.getLogger(__name__)

EVIDENCE_LEN = 120


def _first_match(pattern, text: str) -> str | None:
    m = pattern.search(text or "")
    if not m:
        return None
    start = max(0, m.start() - 40)
    return text[start : m.end() + 40][:EVIDENCE_LEN]


def _standalone_hit(terms: list[str], corpus: str) -> str | None:
    """Guarded match for standalone product strings (INV2).

    Single-token terms use inflection-suffix guards (``diligently``,
    ``cured``, ``galvanized`` never match ``diligent``/``cure``/``galvanize``).
    Multi-word phrases match literally on word boundaries.
    """
    for t in terms:
        if not t:
            continue
        toks = [p for p in re.split(r"\s+", t.strip()) if p]
        if len(toks) == 1:
            rx = brand_token_re(t)
        else:
            inner = r"\s+".join(re.escape(p) for p in toks)
            rx = re.compile(f"\\b{inner}\\b", re.IGNORECASE)
        m = rx.search(corpus)
        if m:
            return m.group(0)
    return None


def adjudicate_candidate(
    base_description: str | None,
    transaction_descriptions: list[str] | None,
    psc_code: str | None,
    manifest: dict[str, Any] | None = None,
    product_terms: list[str] | None = None,
    naics_code: str | None = None,
    recipient_uei: str | None = None,
) -> dict[str, Any]:
    """Classify candidates into TIER_1A / TIER_1B / TIER_1C / DROPPED with rules.

    INV2: every lexical match runs on boilerplate-masked text with
    inflection-guarded token regexes; context-gated-only hits are vetoed by
    domain taxonomy. INV3: entity-homograph UEIs drop unless a standalone
    product token is present; OEM-affiliated sibling-product records go to
    TIER_1C, never Tier 1A/1B.
    """
    raw_corpus = " ".join([base_description or ""] + list(transaction_descriptions or []))
    corpus = mask_boilerplate(raw_corpus)
    for rule, pattern in (
        ("DROP_AUTOCLAVE_POLYMER", DROP_AUTOCLAVE_RE),
        ("DROP_FOOD_RATIONS", DROP_FOOD_RE),
        ("DROP_CORPORATE_COLLISION", DROP_CORP_RE),
        ("DROP_ISR_DOCTRINAL", DROP_ISR_RE),
    ):
        hit = _first_match(pattern, corpus)
        if hit:
            return {"tier": "DROPPED", "rule": rule, "evidence": hit, "competing_oems": []}

    manifest = manifest or {}
    verified_ueis = {u for u in list(manifest.get("verified_oem_ueis")
                                     or manifest.get("oem_family_ueis") or []) if u}
    homograph_ueis = {u for u in list(manifest.get("excluded_homograph_ueis") or []) if u}
    standalone_terms = [t for t in [*list(manifest.get("standalone_keywords", [])),
                        *list(product_terms or [])] if t]
    gated_terms = [t for t in list(manifest.get("context_gated_keywords", [])) if t]
    sibling_rx = None
    if manifest.get("oem_sibling_product_regex"):
        try:
            sibling_rx = re.compile(manifest["oem_sibling_product_regex"], re.I)
        except re.error:
            sibling_rx = None

    def _manifest_rx() -> Any:
        if manifest.get("positive_domain_context_regex"):
            try:
                return re.compile(manifest["positive_domain_context_regex"], re.I)
            except re.error:
                return None
        return None

    manifest_rx = _manifest_rx()
    competitor_rx = None
    if manifest.get("competitor_bundle_regex"):
        try:
            competitor_rx = re.compile(manifest["competitor_bundle_regex"], re.I)
        except re.error:
            competitor_rx = None

    standalone_hit = _standalone_hit(standalone_terms, corpus)
    gated_term_hit = _standalone_hit(gated_terms, corpus) if not standalone_hit else None
    gated_hit = gated_term_hit
    regex_only_hit = None
    competing = sorted({m.lower() for m in TIER_1B_OEM_RE.findall(corpus)})
    if gated_hit is None and not standalone_hit and manifest_rx is not None:
        regex_only_hit = _first_match(manifest_rx, corpus)
        # INV2 adjudication bar: a bare domain noun ("security") with no
        # product/name token anywhere is provisional evidence at best, never
        # Tier-1 confirmation. Return it to the DROPPED pool carrying a
        # distinct rule; dedicated-vehicle lineage promotes it to TIER_2
        # downstream, standalone regex-only records stay dropped.
        if regex_only_hit:
            return {"tier": "DROPPED_POLYSEMOUS_NO_SECURITY_CONTEXT",
                    "rule": "DOMAIN_CONTEXT_ONLY_NO_PRODUCT_TOKEN",
                    "evidence": regex_only_hit, "competing_oems": competing}
        gated_hit = None

    if competitor_rx is not None and (standalone_hit or gated_hit):
        competing = sorted(set(competing) | {m.lower() for m in competitor_rx.findall(corpus)})

    oem_affiliated = bool(verified_ueis) and (recipient_uei or "") in verified_ueis

    # INV3 homograph isolation: an excluded-homograph entity with no
    # standalone product token is not the target, regardless of name overlap.
    if (recipient_uei or "") in homograph_ueis and not standalone_hit:
        return {"tier": DROP_HOMOGRAPH, "rule": "ENTITY_HOMOGRAPH_NO_PRODUCT_TOKEN",
                "evidence": (raw_corpus[:EVIDENCE_LEN] or None), "competing_oems": []}

    # INV2 taxonomy veto: a context-gated-only hit in an excluded taxonomy
    # is dropped unless it explicitly delivers software/hardware.
    if not standalone_hit and gated_hit and taxonomy_vetoed(naics_code, psc_code) \
            and not software_delivery_present(corpus):
        return {"tier": DROP_VETO, "rule": "DOMAIN_TAXONOMY_VETO_GATED_ONLY",
                "evidence": gated_hit, "competing_oems": competing}

    # INV3 sibling-product segregation: OEM-affiliated records evidencing a
    # sibling product line (and no target-scope product token) are TIER_1C.
    if sibling_rx is not None and sibling_rx.search(corpus) and not standalone_hit \
            and not gated_hit and oem_affiliated:
        sib = _first_match(sibling_rx, corpus)
        return {"tier": TIER_1C, "rule": "OEM_SIBLING_PRODUCT_LINE",
                "evidence": sib, "competing_oems": competing}

    if standalone_hit:
        tier = TIER_1B if competing else TIER_1A
        return {"tier": tier, "rule": "KEEP_MANIFEST_PRODUCT_MATCH", "evidence": standalone_hit,
                "competing_oems": competing}
    if gated_hit:
        tier = TIER_1B if competing else TIER_1A
        return {"tier": tier, "rule": "KEEP_GATED_CONTEXT_MATCH", "evidence": gated_hit,
                "competing_oems": competing}
    direct_hit = _first_match(KEEP_DIRECT_RE, corpus)
    if direct_hit:
        tier = TIER_1B if competing else TIER_1A
        return {"tier": tier, "rule": "KEEP_DIRECT_PRODUCT_MATCH", "evidence": direct_hit,
                "competing_oems": competing}
    if KEEP_POLY_TOKEN_RE.search(corpus) and (
        SECURITY_CONTEXT_RE.search(corpus) or (psc_code or "") in ("J063", "N063", "6350")
    ):
        tier = TIER_1B if competing else TIER_1A
        return {"tier": tier, "rule": "KEEP_POLYSEMOUS_WITH_SECURITY_CONTEXT",
                "evidence": _first_match(KEEP_POLY_TOKEN_RE, corpus),
                "competing_oems": competing}
    return {"tier": "DROPPED_POLYSEMOUS_NO_SECURITY_CONTEXT", "rule": "POLY_NO_CONTEXT",
            "evidence": (raw_corpus[:EVIDENCE_LEN] or None), "competing_oems": competing}


def register_adjudicate_lexical_matches(mcp: FastMCP, client: USASpendingClient) -> None:
    @mcp.tool(
        name="adjudicate_lexical_matches",
        annotations={"title": "Contextual lexical adjudication with evidence tiers",
                     "readOnlyHint": True, "destructiveHint": False,
                     "idempotentHint": True, "openWorldHint": True},
    )
    async def adjudicate_lexical_matches(
        candidates: Annotated[
            list[dict[str, Any]],
            Field(description="Each: {gid, base_description, transaction_descriptions[], psc_code}."),
        ],
        manifest: Annotated[
            dict[str, Any] | None,
            Field(description="Stage 0 SearchScopeManifest; its standalone literals + positive regex also confer TIER_1 (generalizes beyond PACS)."),
        ] = None,
        product_terms: Annotated[
            list[str] | None,
            Field(description="Extra product literals conferring TIER_1."),
        ] = None,
    ) -> dict[str, Any]:
        """Classify candidates into TIER_1A / TIER_1B / TIER_2-pending / DROPPED with rules."""
        results = []
        for cand in candidates:
            verdict = adjudicate_candidate(
                cand.get("base_description"),
                cand.get("transaction_descriptions") or [],
                cand.get("psc_code"),
                manifest=manifest,
                product_terms=product_terms,
                naics_code=cand.get("naics_code"),
                recipient_uei=cand.get("recipient_uei"),
            )
            verdict["gid"] = cand.get("gid")
            results.append(verdict)
        return {"results": results}
