"""v5 systemic invariants — domain-agnostic shared constants and pure functions.

Every rule here is structural: it refers to morphology, taxonomy, graph
membership, or accounting cohorts — never to a specific vendor, product,
or PIID prefix. Target-specific patches are prohibited from reappearing
disguised as new code; new collisions are handled by these mechanisms.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------
# INV2: morphological inflection + FAR boilerplate guards
# --------------------------------------------------------------------------

#: Inflectional suffixes that turn a brand/product token into a derived
#: English word that is never the brand (adverbs, participles, gerunds,
#: deadjectival nouns/adjectives). Applied generically to every brand token.
INFLECT_SUFFIXES = ("ly", "ed", "ing", "ness", "ment", "able", "al")


def brand_token_re(token: str) -> re.Pattern[str]:
    """Regex matching `token` as a standalone word, rejecting derived
    inflected forms (``diligently``, ``cured``, ``galvanized`` ...).

    Multi-word terms anchor each word; the suffix guard applies to the
    final token (``Diligent One`` still matches ``Diligent One`` but the
    single-token path is what REG-03 exercises).
    """
    parts = [re.escape(p) for p in re.split(r"\s+", token.strip()) if p]
    if not parts:
        return re.compile(r"(?!)")
    if len(parts) == 1:
        suffix = "|".join(INFLECT_SUFFIXES)
        return re.compile(f"\\b{parts[0]}(?!(?:{suffix})\\b)\\b", re.IGNORECASE)
    body = r"\s+".join(parts[:-1])
    last = parts[-1]
    suffix = "|".join(INFLECT_SUFFIXES)
    return re.compile(f"\\b{body}\\s+{last}(?!(?:{suffix})\\b)\\b", re.IGNORECASE)


#: Federal contracting boilerplate + legal-process phrases that must never
#: confer product scope. Masked (blanked) before lexical adjudication so
#: no brand stem inside them can match.
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

_BOILERPLATE_RES = [re.compile(re.escape(p), re.IGNORECASE) for p in BOILERPLATE_PHRASES]


def mask_boilerplate(text: str | None) -> str:
    """Replace boilerplate phrases with spaces (length-preserving-ish)."""
    t = text or ""
    for rx in _BOILERPLATE_RES:
        t = rx.sub(" ", t)
    return t


# --------------------------------------------------------------------------
# INV1/INV2: domain taxonomy + sibling nouns
# --------------------------------------------------------------------------

#: NAICS prefixes that veto a context-gated product claim (construction,
#: raw materials, manufacturing except 334 electronics, food, apparel).
TAXONOMY_EXCLUDED_NAICS_PREFIXES = (
    "236", "237", "238",            # construction
    "311", "312", "313", "314", "315", "316",  # food/textiles/apparel/leather
    "321", "322", "323", "324", "325", "326", "327",  # wood/paper/petro/chem/plastics
    "331", "332", "333",            # metals/machinery (non-electronic)
)

#: PSC prefixes that veto a context-gated product claim.
TAXONOMY_EXCLUDED_PSC_PREFIXES = ("Y1", "Z1", "Z2")

#: Nouns evidencing software/hardware delivery (used for Signal B
#: co-occurrence and for the taxonomy-veto escape hatch).
SOFTWARE_DELIVERY_NOUNS = (
    "software", "license", "licence", "subscription", "firmware", "saas",
    "server", "panel", "controller", "reader", "platform", "analytics",
    "maintenance", "renewal", "upgrade",
)

#: Broader domain sibling nouns for triage Signal B (delivery nouns plus
#: adjacent security nouns). All lowercase, matched as whole words.
#: "access"/"control" are the domain descriptors of access-control scope
#: (IT "access control list" collisions are caught by negative drop-filters,
#: which run before sibling matching).
SIBLING_DOMAIN_NOUNS = frozenset(SOFTWARE_DELIVERY_NOUNS + (
    "security", "pacs", "access", "control", "badge", "door", "alarm", "camera",
    "surveillance", "intrusion", "intercom", "turnstile", "visitor", "grc",
    "audit",
))

_SIBLING_RE = re.compile(f"\\b({'|'.join(sorted(SIBLING_DOMAIN_NOUNS))})\\b", re.IGNORECASE)
_DELIVERY_RE = re.compile(f"\\b({'|'.join(sorted(SOFTWARE_DELIVERY_NOUNS))})\\b", re.IGNORECASE)


def sibling_nouns_present(text: str | None) -> list[str]:
    """Distinct domain sibling nouns present in `text` (whole-word)."""
    return sorted({m.lower() for m in _SIBLING_RE.findall(text or "")})


def software_delivery_present(text: str | None) -> bool:
    """Whether `text` names a software/hardware delivery noun."""
    return bool(_DELIVERY_RE.search(text or ""))


def taxonomy_vetoed(naics: str | None, psc: str | None) -> bool:
    """True when NAICS/PSC falls in an excluded physical/construction/food
    taxonomy. Empty/unknown taxonomy never vetoes (neutral)."""
    n = (naics or "").strip()
    if n and n[:3] in TAXONOMY_EXCLUDED_NAICS_PREFIXES:
        return True
    p = (psc or "").strip().upper()
    return bool(p and any(p.startswith(x) for x in TAXONOMY_EXCLUDED_PSC_PREFIXES))


# --------------------------------------------------------------------------
# INV1: high-collision token ceiling
# --------------------------------------------------------------------------

#: Short stems with known doctrinal/industry homographs. Not common English
#: words, but structurally collision-prone (e.g. iSTAR vs. DoD
#: Intelligence-Surveillance-Target Acquisition-Reconnaissance / iStar
#: Financial). Never standalone.
KNOWN_COLLISION_STEMS = frozenset({"istar", "isr", "ehr"})

#: Common English words that must never run as standalone triggers
#: (single-token brand collisions; multi-word phrases containing these
#: tokens are evaluated as a whole and are NOT capped by this set).
COMMON_WORDS = frozenset(["diligent", "diligently", "diligence", "cure", "cures", "cured", "curing", "galvanize", "galvanized", "boards", "board", "effect", "effects", "one", "pro", "edge", "ultra", "analytics", "analytic", "software", "house", "international", "systems", "services", "solutions", "technology", "security", "access", "control", "panel", "reader", "server", "platform", "maintenance"])


def _single_token(term: str) -> str:
    return re.sub(r"[\s\-_*.•/]+", "", term).lower()


def is_high_collision_token(term: str, multi_entity: bool = False) -> bool:
    """True when `term` may never be STANDALONE_SAFE.

    - single token of <= 4 characters (acronyms/stems), OR
    - single lowercase token that is a common English word/stem, OR
    - single lowercase token in KNOWN_COLLISION_STEMS (doctrinal homographs), OR
    - observed in >= 2 distinct corporate families (multi_entity).
    """
    if multi_entity:
        return True
    t = term.strip()
    if not t:
        return True
    if " " not in t and len(_single_token(t)) <= 4:
        # Single short token (acronyms like ACL/ISR/EHR, stems like CURE).
        # Multi-word phrases skip the length rule even if they contain a
        # short token ("ACL Analytics" is judged as a phrase).
        return True
    compact = t.lower()
    return " " not in compact and (compact in COMMON_WORDS or compact in KNOWN_COLLISION_STEMS)


def strip_term_variants(text: str, term: str) -> str:
    """Remove every orthographic variant of `term` from `text` so the
    orthogonal check cannot self-confirm on the retrieval trigger."""
    t = text or ""
    variants = {term, _single_token(term)}
    squished = re.sub(r"[\s\-_*.•/]+", "", term)
    variants.add(squished)
    for v in sorted(variants, key=len, reverse=True):
        if len(v) >= 3:
            t = re.compile(re.escape(v), re.IGNORECASE).sub(" ", t)
    # Also strip bare version numbers attached to the term (9000-class
    # part-number stems must not self-confirm).
    m = re.search(r"(\d{2,})$", squished)
    if m:
        t = re.compile(f"\\b{re.escape(m.group(1))}\\b").sub(" ", t)
    return t


# --------------------------------------------------------------------------
# INV5/INV6: accounting + ledger enums
# --------------------------------------------------------------------------

#: Non-empty historical reason enum (INV6: every Section 4 row carries one).
HISTORICAL_REASONS = (
    "TERMINATED_MOD_C1_C4",
    "ZERO_OR_NEGATIVE_LIFETIME",
    "ENDED_PRE_WINDOW_ZERO_IN_WINDOW",
    "IN_WINDOW_NET_DEOBLIGATION",
)

TIER_1A = "TIER_1A_CONFIRMED_PURE"
TIER_1B = "TIER_1B_CONFIRMED_BUNDLED_MULTI_OEM"
TIER_1C = "TIER_1C_OEM_SIBLING_PRODUCT"
TIER_2 = "TIER_2_VEHICLE_ATTRIBUTED_PROVISIONAL"
TIER_3 = "TIER_3_BROAD_VEHICLE_EXCLUDED"
HISTORICAL = "HISTORICAL_OR_DEOBLIGATED"
DROP_HOMOGRAPH = "DROPPED_ENTITY_HOMOGRAPH"
DROP_VETO = "DROPPED_DOMAIN_VETO"
