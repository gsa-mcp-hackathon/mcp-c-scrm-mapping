"""Stage 4 — lexical adjudication (manifest-governed, no embedded vendor lexicon).

Deliberate divergence from the legacy package: the legacy KEEP_DIRECT_RE /
TIER_1B_OEM_RE / SECURITY_CONTEXT_RE patterns embed prior targets' vendor
vocabulary (c-cure, istar, lenel, genetec ...). Per the independence rule,
this package matches ONLY manifest terms, with generic structural guards:
- inflection-guarded whole-word matching (diligently never matches diligent);
- FAR boilerplate masking (construction/legal-process phrases);
- domain taxonomy veto for gated-only hits (construction NAICS, Y1/Z1/Z2 PSC);
- entity-homograph drop via manifest UEI exclusions;
- sibling-product segregation for conglomerate targets (TIER_1C);
- conglomerate name-only retention prohibited (Fix 2 structural form).

Provisional rule: manifest domain-context-only hits (positive_regex with no
product token) are DROPPED here; dedicated-vehicle lineage promotes them to
Tier-2 downstream, standalone regex-only records stay dropped.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from usaspending_cscrm_mcp.evidence import Evidence
from usaspending_cscrm_mcp.models import AwardRecord, MonetaryBasis
from usaspending_cscrm_mcp.pipeline.stage0_triage import (
    TAXONOMY_EXCLUDED_PREFIXES,
)

EVIDENCE_LEN = 240

INFLECT_SUFFIXES = ("ly", "ed", "ing", "ness", "ment", "able", "al")
EXCLUDED_PSC_PREFIXES = ("Y1", "Z1", "Z2")
SOFTWARE_DELIVERY_NOUNS = (
    "software", "license", "licence", "subscription", "firmware", "saas",
    "module", "platform", "portal", "dashboard",
)

BOILERPLATE_PHRASES = (
    "prosecute the work diligently",
    "diligent effort",
    "diligent efforts",
    "due diligence",
    "cure notice",
    "cure notices",
    "right to cure",
    "time is of the essence",
    "best efforts",
    "good faith effort",
)
_BOILERPLATE_RES = [re.compile(re.escape(p), re.I) for p in BOILERPLATE_PHRASES]

_DELIVERY_RE = re.compile(r"\b(" + "|".join(sorted(SOFTWARE_DELIVERY_NOUNS)) + r")\b", re.I)


class Adjudication(BaseModel):
    gid: str
    tier: str
    display: str = "DROPPED"
    rule: str = ""
    basis: MonetaryBasis = "vendor_associated"
    snippet: str = ""
    competing: list[str] = Field(default_factory=list)
    dedicated_lineage: bool = False


def mask_boilerplate(text: str | None) -> str:
    t = text or ""
    for rx in _BOILERPLATE_RES:
        t = rx.sub(" ", t)
    return t


def term_re(term: str) -> re.Pattern[str]:
    """Whole-word term regex with a generic inflection guard on the final token."""
    parts = [re.escape(p) for p in re.split(r"\s+", term.strip()) if p]
    if not parts:
        return re.compile(r"(?!)")
    suffix = "|".join(INFLECT_SUFFIXES)
    if len(parts) == 1:
        return re.compile(rf"\b{parts[0]}(?!(?:{suffix})\b)\b", re.I)
    body = r"\s+".join(parts[:-1])
    return re.compile(rf"\b{body}\s+{parts[-1]}(?!(?:{suffix})\b)\b", re.I)


def _first_match(rx: re.Pattern[str], corpus: str) -> str | None:
    m = rx.search(corpus or "")
    return m.group(0) if m else None


def taxonomy_vetoed(naics: str | None, psc: str | None) -> bool:
    n, p = (naics or ""), (psc or "")
    return n[:3] in TAXONOMY_EXCLUDED_PREFIXES or p[:2] in EXCLUDED_PSC_PREFIXES


def software_delivery_present(corpus: str) -> bool:
    return bool(_DELIVERY_RE.search(corpus or ""))


def adjudicate(record: AwardRecord, *, standalone_terms: list[str],
               gated_terms: list[str], verified_ueis: set[str],
               homograph_ueis: set[str], domain_context_rx: str | None = None,
               competitor_rx: str | None = None,
               is_conglomerate: bool = False,
               allow_name_only: bool = False,
               dedicated_lineage: bool = False,
               sibling_terms: list[str] | None = None,
               target_name: str = "") -> Adjudication:
    """Classify one hydrated award. Manifest terms only — no vendor lexicon.

    Tier policy:
    - standalone target hit alone -> Tier-1A (Tier-1B when competitor OEMs
      co-occur).
    - standalone target hit + sibling hit -> Tier-1B bundle
      (KEEP_BUNDLE_TARGET_PLUS_SIBLING): same-OEM bundles are entitlement
      evidence, never pure-target spend.
    - sibling hit + target product-token hit (no standalone hit, e.g.
      "CONNECT AND PROTECT") -> Tier-1B bundle
      (KEEP_BUNDLE_SIBLING_PLUS_PRODUCT_TOKEN).
    - sibling hit only -> Tier-1C (OEM_SIBLING_PRODUCT_LINE), never Tier-1A.
    - gated-only hit (no standalone/sibling token) -> Tier-2 provisional,
      never Tier-1A: brand-stem evidence is unresolved product attribution.
    - vendor-level query (single-word target) + verified-OEM recipient with
      no token hit -> Tier-1A OEM_DIRECT_VERIFIED_PRIME: a prime contract
      directly with the target vendor is vendor exposure by construction.
      Never applies to product-level (multi-word target) queries, where a
      verified recipient alone cannot confirm the product.
    """
    corpus = mask_boilerplate(
        " ".join([record.base_description, *record.transaction_descriptions]))

    def compile_rx(pattern: str | None) -> re.Pattern[str] | None:
        if not pattern:
            return None
        try:
            return re.compile(pattern, re.I)
        except re.error:
            return None

    manifest_rx, compet_rx = compile_rx(domain_context_rx), compile_rx(competitor_rx)

    def standalone_hit() -> str | None:
        for t in standalone_terms:
            if not t:
                continue
            hit = _first_match(term_re(t), corpus)
            if hit:
                return hit
        return None

    def gated_hit() -> str | None:
        for t in gated_terms:
            if not t:
                continue
            hit = _first_match(term_re(t), corpus)
            if hit:
                return hit
        return None

    competing: list[str] = []
    if compet_rx is not None:
        competing = sorted({m.lower() for m in compet_rx.findall(corpus)})

    s_hit = standalone_hit()
    g_hit = gated_hit() if not s_hit else None

    siblings = [t for t in (sibling_terms or []) if t and t.strip()]
    sib_hit: str | None = None
    for t in siblings:
        hit = _first_match(term_re(t), corpus)
        if hit:
            sib_hit = hit
            break
    product_token_hit: str | None = None
    target_words = [w for w in re.split(r"\s+", (target_name or "").strip()) if w]
    if target_words:
        product_token_hit = _first_match(term_re(target_words[-1]), corpus)

    # Homograph isolation: excluded-homograph entity with no standalone token
    # is not the target, regardless of name overlap.
    if record.recipient_uei in homograph_ueis and not s_hit:
        return Adjudication(gid=record.gid, tier="DROPPED",
                            rule="ENTITY_HOMOGRAPH_NO_PRODUCT_TOKEN",
                            snippet=corpus[:EVIDENCE_LEN],
                            dedicated_lineage=dedicated_lineage)

    # Vendor-query OEM-direct rule: a prime directly with the verified
    # target vendor is vendor exposure by construction, and recipient
    # verification outranks description tokens here (a direct prime whose
    # description also names the brand is still a direct prime). Product-level
    # queries never reach this branch (a verified recipient alone cannot
    # confirm the product). Homograph-excluded UEIs never match (verified and
    # excluded sets are disjoint by construction). Competing-OEM mentions
    # still ride along in `competing`, so AT-05 fails closed on true bundles.
    vendor_query = len(target_words) == 1
    oem_direct = (vendor_query and record.recipient_uei
                  and record.recipient_uei in set(verified_ueis or ()))
    if oem_direct:
        who = (record.recipient_name or record.recipient_uei or "").strip()
        desc = (record.base_description or "").strip()[:160]
        return Adjudication(gid=record.gid, tier="Tier-1A",
                            rule="OEM_DIRECT_VERIFIED_PRIME",
                            basis="vendor_associated",
                            snippet=f"{who} :: {desc}" if desc else who,
                            competing=competing,
                            dedicated_lineage=dedicated_lineage)

    # Conglomerate name-only retention prohibited.
    if is_conglomerate and not allow_name_only and not s_hit and not g_hit \
            and not software_delivery_present(corpus):
        return Adjudication(gid=record.gid, tier="DROPPED",
                            rule="CONGLOMERATE_NAME_ONLY_NO_PRODUCT_TEXT",
                            snippet=corpus[:EVIDENCE_LEN],
                            dedicated_lineage=dedicated_lineage)

    # Taxonomy veto for gated-only hits without software/hardware delivery.
    if not s_hit and g_hit and taxonomy_vetoed(record.naics_code, record.psc_code) \
            and not software_delivery_present(corpus):
        return Adjudication(gid=record.gid, tier="DROPPED",
                            rule="DOMAIN_TAXONOMY_VETO_GATED_ONLY",
                            snippet=g_hit, competing=competing,
                            dedicated_lineage=dedicated_lineage)

    # Sibling-product segregation (unconditional when the manifest carries
    # sibling terms): sibling hits are never pure-target evidence.
    # Target+sibling co-match (or sibling + target product token, e.g.
    # "CONNECT AND PROTECT") is same-OEM bundle entitlement -> Tier-1B.
    # Sibling-only is Tier-1C context. This replaces the old
    # conglomerate-gated OEM_SIBLING block, which never fired for pure-play
    # runs and let sibling rows reach Tier-1A.
    if sib_hit and s_hit:
        return Adjudication(gid=record.gid, tier="Tier-1B",
                            rule="KEEP_BUNDLE_TARGET_PLUS_SIBLING",
                            basis="product_associated",
                            snippet=f"{s_hit} + {sib_hit}",
                            competing=competing,
                            dedicated_lineage=dedicated_lineage)
    if sib_hit and not s_hit and product_token_hit:
        return Adjudication(gid=record.gid, tier="Tier-1B",
                            rule="KEEP_BUNDLE_SIBLING_PLUS_PRODUCT_TOKEN",
                            basis="product_associated",
                            snippet=f"{sib_hit} + {product_token_hit}",
                            competing=competing,
                            dedicated_lineage=dedicated_lineage)
    if sib_hit and not s_hit:
        return Adjudication(gid=record.gid, tier="Tier-1C",
                            rule="OEM_SIBLING_PRODUCT_LINE",
                            basis="vendor_associated", snippet=sib_hit,
                            dedicated_lineage=dedicated_lineage)

    if s_hit:
        tier = "Tier-1B" if competing else "Tier-1A"
        return Adjudication(gid=record.gid, tier=tier,
                            rule="KEEP_MANIFEST_PRODUCT_MATCH",
                            basis="product_associated", snippet=s_hit,
                            competing=competing, dedicated_lineage=dedicated_lineage)
    if g_hit:
        # Gated-only evidence (brand stem, no target/sibling token) is
        # unresolved product attribution: Tier-2 provisional, never Tier-1A.
        # Dedicated-vehicle lineage keeps the Tier-2 vehicle-attributed form.
        return Adjudication(gid=record.gid, tier="Tier-1B" if competing else "Tier-2",
                            rule="KEEP_GATED_CONTEXT_MATCH",
                            basis="product_associated", snippet=g_hit,
                            competing=competing, dedicated_lineage=dedicated_lineage)
    if manifest_rx is not None:
        rx_hit = _first_match(manifest_rx, corpus)
        if rx_hit:
            return Adjudication(gid=record.gid, tier="DROPPED",
                                rule="DOMAIN_CONTEXT_ONLY_NO_PRODUCT_TOKEN",
                                snippet=rx_hit, competing=competing,
                                dedicated_lineage=dedicated_lineage)
    return Adjudication(gid=record.gid, tier="DROPPED", rule="POLY_NO_CONTEXT",
                        snippet=corpus[:EVIDENCE_LEN],
                        dedicated_lineage=dedicated_lineage)


def apply_display(adjudication: Adjudication, record: AwardRecord) -> Adjudication:
    """Ghost rule display mapping: historical presentation preserves tier."""
    if record.ghost_historical:
        adjudication.display = "HISTORICAL"
    else:
        adjudication.display = adjudication.tier
    return adjudication


def to_evidence(adjudication: Adjudication, record: AwardRecord,
                rule_id: str = "") -> Evidence:
    """Attach typed lexical evidence to an adjudication."""
    return Evidence(kind="lexical", rule_id=rule_id or adjudication.rule,
                    exact_substring=adjudication.snippet,
                    field_path="base_description+transaction_descriptions",
                    source_reference=record.gid,
                    substring_verified=bool(
                        adjudication.snippet
                        and adjudication.snippet in mask_boilerplate(
                            " ".join([record.base_description,
                                      *record.transaction_descriptions]))))
