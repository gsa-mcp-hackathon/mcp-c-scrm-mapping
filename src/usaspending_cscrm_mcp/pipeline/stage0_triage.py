"""Stage 0 — triage & deconfliction (proven v5 algorithm, new foundations).

Orthogonal precision: the retrieval trigger is stripped before scoring; a row
counts as a True Positive only via an independent signal (A: verified UEI-graph
membership for ARCHETYPE_1 targets; B: domain taxonomy + sibling-noun
co-occurrence). Short-stem/high-collision triggers never go standalone.
Conglomerate/division-DBA targets: name-only retention prohibited; product
text or dedicated lineage required for Tier-1/2 eligibility.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from usaspending_cscrm_mcp import evidence as evidence_mod
from usaspending_cscrm_mcp.client import USAspendingClient
from usaspending_cscrm_mcp.execution import (
    BudgetTracker,
    ContinuationToken,
    RunBudgets,
    check_scope_binding,
    mint_continuation,
    new_run_id,
)

SAMPLE_FIELDS = [
    "Award ID", "Recipient Name", "Recipient UEI", "Parent Recipient UEI",
    "Awarding Agency", "Funding Agency", "Award Amount", "Total Outlays",
    "Description", "Contract Award Type", "NAICS Code", "NAICS Description",
    "PSC Code", "PSC Description", "Start Date", "End Date",
    "generated_internal_id",
]

# Proven guards (port of the verified v4/v5 invariant set — no target-specific rules).
TAXONOMY_EXCLUDED_PREFIXES = ("236", "237", "238")
DROP_HOMOGRAPHS = frozenset({"ham", "hams", "cure", "cured", "cures", "curing", "diligent"})
SIBLING_NOUNS = (
    "software", "platform", "subscription", "license", "governance",
    "module", "portal", "dashboard", "analytics", "suite", "application",
)
DELIVERY_NOUNS = SIBLING_NOUNS + ("training", "course", "certification", "bootcamp")
MASK_BOILERPLATE_RES = (
    re.compile(r"\bFAR\b.{0,80}?(?:construction|janitorial|custodial)\b", re.I),
    re.compile(r"\b52\.211-10\b"),
    re.compile(r"\b(?:commencement|prosecution|completion|physical progress)\b.{0,40}?\bwork\b", re.I),
)

Disposition = Literal["STANDALONE", "CONTEXT_GATED", "REJECTED"]


class TermScore(BaseModel):
    term: str
    sample_size: int = 0
    total_count: int = 0
    true_positives: int = 0
    precision: float = 0.0
    disposition: Disposition = "REJECTED"
    signals: dict[str, int] = Field(default_factory=dict)


class TargetManifest(BaseModel):
    """Locked Stage-0 contract. Downstream stages may not widen its scope."""

    model_config = {"arbitrary_types_allowed": True}

    manifest_version: str = "2.0"
    target_name: str
    input_hash: str
    window_start: str
    window_end: str
    standalone_terms: list[str] = Field(default_factory=list)
    context_gated_terms: list[str] = Field(default_factory=list)
    rejected_terms: list[str] = Field(default_factory=list)
    term_scores: list[TermScore] = Field(default_factory=list)
    positive_regex: str = ""
    negative_regex: str = ""
    verified_oem_ueis: list[str] = Field(default_factory=list)
    excluded_homograph_ueis: list[str] = Field(default_factory=list)
    domain_nouns: list[str] = Field(default_factory=list)
    sibling_terms: list[str] = Field(default_factory=list)
    conglomerate_signals: list[str] = Field(default_factory=list)
    effective_archetype: str = "ARCHETYPE_1_PURE_PLAY_VENDOR"
    is_conglomerate: bool = False
    target_archetype: str = "ARCHETYPE_1_PURE_PLAY_VENDOR"
    allow_conglomerate_recipient_search: bool = False
    force_vehicle_class: str | None = None
    sampling_policy: str = "25-row pre-flight per candidate term, sorted Award Amount desc"
    domain_taxonomy: list[str] = Field(default_factory=list)
    unresolved_limitations: list[str] = Field(default_factory=list)
    rule_versions: dict[str, str] = Field(
        default_factory=lambda: {"orthogonal_precision": "v5",
                                 "collision_ceiling": "v5",
                                 "conglomerate_gate": "v5"})
    status: Literal["READY", "NEEDS_USER_INPUT"] = "READY"

    def canonical_hash(self) -> str:
        return evidence_mod.content_hash(self.model_dump(mode="json"))


def strip_trigger(text: str, keyword: str) -> str:
    """Remove the retrieval trigger before scoring (anti-tautology)."""
    return re.sub(re.escape(keyword), " ", text or "", flags=re.I)


def score_row(row: dict[str, Any], keyword: str, verified_ueis: set[str],
              is_pure_play: bool, sibling_nouns: tuple[str, ...] = SIBLING_NOUNS,
              domain_nouns: tuple[str, ...] = ()
              ) -> tuple[bool, str]:
    """Return (is_true_positive, signal). Orthogonal: trigger pre-stripped."""
    text = " ".join(str(row.get(f, "") or "") for f in
                    ("Description", "NAICS Description", "PSC Description"))
    stripped = strip_trigger(text, keyword).lower()
    for rx in MASK_BOILERPLATE_RES:
        stripped = rx.sub(" ", stripped)
    tokens = set(re.findall(r"[a-z0-9]+", stripped))
    naics = str(row.get("NAICS Code", "") or "")
    uei = str(row.get("Recipient UEI", "") or "")
    if uei and uei in verified_ueis and is_pure_play:
        return True, "A"
    if naics[:3] in TAXONOMY_EXCLUDED_PREFIXES:
        return False, "none"
    nouns = set(sibling_nouns) | {n.lower() for n in domain_nouns if n}
    sib_hit = any(s in tokens for s in nouns)
    # NOTE: the term stem is intentionally NOT required here. Retrieval already
    # guarantees the trigger matched; re-requiring it after stripping is either
    # tautological (single-word terms) or impossible (multi-word/hyphenated
    # terms whose parts the strip removed). The sibling noun is the orthogonal
    # confirmation. Delivery-only nouns (training/course/...) never confirm.
    if sib_hit:
        return True, "B"
    return False, "none"


def dispose_term(term: str, precision: float, *, sibling_candidate: bool = False) -> Disposition:
    """Spec Exhibit A cutoffs: STANDALONE >= 0.85, CONTEXT_GATED >= 0.15.

    High-collision ceiling: short/acronym/common/homograph stems never go
    standalone. Sibling candidates (non-target products sharing the vendor
    stem) are capped at CONTEXT_GATED even at high precision — Stage 4
    demotes their hits to Tier-1C / bundle tiers.
    """
    t = term.strip().lower()
    if t in DROP_HOMOGRAPHS or len(t) <= 3 or " " not in t and len(t) <= 5:
        return "CONTEXT_GATED" if precision >= 0.15 else "REJECTED"
    if sibling_candidate:
        return "CONTEXT_GATED" if precision >= 0.15 else "REJECTED"
    if precision >= 0.85:
        return "STANDALONE"
    if precision >= 0.15:
        return "CONTEXT_GATED"
    return "REJECTED"


def detect_sibling_terms(target_name: str,
                          candidate_terms: list[str]) -> list[str]:
    """Structural sibling detection — no vendor lexicon.

    For a product-specific query (multi-word target), other candidate terms
    sharing the leading vendor stem token are sibling-product candidates
    (e.g. target 'Jamf Protect' vs candidate 'Jamf Connect'). Single-word
    (vendor/brand-level) targets never produce siblings: every term is
    in-scope for vendor exposure.
    """
    target = (target_name or "").strip().lower()
    words = target.split()
    if len(words) < 2:
        return []
    stem = words[0]
    siblings: list[str] = []
    for term in candidate_terms:
        t = (term or "").strip()
        tl = t.lower()
        if not t or tl == target:
            continue
        parts = tl.split()
        # Single-word terms are umbrella brand stems (gated brand evidence),
        # not sibling products.
        if len(parts) < 2 or parts[0] != stem:
            continue
        siblings.append(t)
    return siblings


def detect_conglomerate_triggers(candidate_terms: list[str], recipient_names: list[str],
                                 uei_count: int) -> list[str]:
    triggers: list[str] = []
    for term in candidate_terms:
        words = term.lower().split()
        if len(words) >= 2 and any(len(w) <= 3 for w in words):
            triggers.append(f"acronym_or_short_token:{term}")
    if uei_count > 5:
        triggers.append("diverse_uei_footprint")
    if any("dba" in (n or "").lower() for n in recipient_names):
        triggers.append("dba_alias_present")
    return triggers


_PROBE_GROUPS: tuple[tuple[str, list[str]], ...] = (
    ("contracts", ["A", "B", "C", "D"]),
    ("idvs", ["IDV_A", "IDV_B", "IDV_C", "IDV_D", "IDV_E"]),
)


async def probe_term(client: USAspendingClient, term: str, verified_ueis: set[str],
                     is_pure_play: bool, from_date: str, to_date: str,
                     budgets: RunBudgets, tracker: BudgetTracker,
                     sibling_candidate: bool = False,
                     recipient_names: list[str] | None = None,
                     domain_nouns: tuple[str, ...] = ()) -> TermScore:
    """25-row pre-flight probe per vehicle group, unioned by GID.

    spending_by_award 422s mixed contract+IDV code sets, so the probe
    dispatches paired single-group calls — the same split Stage-1
    discovery uses — instead of one mixed call that always fails.
    award_type_codes are mandatory: keyword searches without them 422
    (verified live 2026-11-01).
    """
    if tracker.exhausted():
        return TermScore(term=term)
    rows: list[dict[str, Any]] = []
    total = 0
    seen: set[str] = set()
    for _group, codes in _PROBE_GROUPS:
        if tracker.exhausted():
            break
        tracker.pages += 1
        payload = {
            "filters": {"keywords": [term],
                        "award_type_codes": codes,
                        "time_period": [
                            {"start_date": from_date, "end_date": to_date}]},
            "fields": SAMPLE_FIELDS,
            "limit": 25,
            "page": 1,
            "sort": "Award Amount",
            "order": "desc",
        }
        try:
            data = await client.post("search/spending_by_award/", payload)
        except Exception:  # noqa: BLE001 - probe failure yields a zero-precision score
            continue
        batch = data.get("results", []) or []
        try:
            count_data = await client.post(
                "search/spending_by_award_count/",
                {"filters": {"keywords": [term], "award_type_codes": codes,
                             "time_period": [{"start_date": from_date,
                                              "end_date": to_date}]}})
            total += sum(int(v) for v in
                         (count_data.get("results", {}) or {}).values()
                         if isinstance(v, int))
        except Exception:  # noqa: BLE001 - count anchor best-effort
            pass
        for r in batch:
            gid = r.get("generated_internal_id") if isinstance(r, dict) else None
            if gid is not None and gid not in seen:
                seen.add(gid)
                rows.append(r)
                if recipient_names is not None and isinstance(r, dict):
                    nm = r.get("Recipient Name")
                    if nm:
                        recipient_names.append(str(nm))
    tp = sum(1 for r in rows
             if score_row(r if isinstance(r, dict) else {}, term,
                          verified_ueis, is_pure_play,
                          domain_nouns=domain_nouns)[0])
    precision = (tp / len(rows)) if rows else 0.0
    return TermScore(term=term, sample_size=len(rows), total_count=total,
                     true_positives=tp, precision=round(precision, 4),
                     disposition=dispose_term(term, precision,
                                              sibling_candidate=sibling_candidate))


async def triage_target(client: USAspendingClient, *, target_name: str,
                        candidate_terms: list[str], verified_oem_ueis: list[str],
                        target_archetype: str = "ARCHETYPE_1_PURE_PLAY_VENDOR",
                        product_terms: list[str] | None = None,
                        negative_terms: list[str] | None = None,
                        domain_nouns: list[str] | None = None,
                        from_date: str, to_date: str,
                        budgets: RunBudgets | None = None,
                        signing_key: bytes | None = None,
                        continuation: str | None = None,
                        caller_id: str = "local") -> dict[str, Any]:
    """Stage 0. Returns {status, manifest, manifest_hash, continuation?}.

    Probing is sequential and cheap (one page per term); if budgets exhaust
    mid-probe the stage returns INCOMPLETE with a continuation cursor.
    """
    budgets = budgets or RunBudgets()
    tracker = BudgetTracker(budgets)
    run_id = new_run_id()
    start_at = 0
    if continuation and signing_key:
        ct = ContinuationToken.decode(continuation, signing_key)
        check_scope_binding(ct, caller_id, "stage0:" + target_name, "probe")
        run_id = ct.run_id
        start_at = int(ct.cursor.get("term_index", 0))
    verified = set(verified_oem_ueis)
    is_pure = target_archetype == "ARCHETYPE_1_PURE_PLAY_VENDOR"
    nouns = tuple(n for n in (domain_nouns or []) if n and n.strip())
    sibling_set = {s.lower() for s in detect_sibling_terms(target_name, candidate_terms)}
    recipient_names: list[str] = []
    scores: list[TermScore] = []
    for idx, term in enumerate(candidate_terms[start_at:], start=start_at):
        if tracker.exhausted():
            if signing_key is None:
                return {"status": "INCOMPLETE", "reason": tracker.exhausted(),
                        "manifest": None, "continuation": None}
            cont = mint_continuation(run_id, caller_id, "stage0:" + target_name,
                                     "probe", {"term_index": idx}, signing_key)
            return {"status": "INCOMPLETE", "reason": tracker.exhausted(),
                    "manifest": None, "continuation": cont}
        scores.append(await probe_term(client, term, verified, is_pure,
                                       from_date, to_date, budgets, tracker,
                                       sibling_candidate=term.strip().lower() in sibling_set,
                                       recipient_names=recipient_names,
                                       domain_nouns=nouns))
    standalone = [s.term for s in scores if s.disposition == "STANDALONE"]
    gated = [s.term for s in scores if s.disposition == "CONTEXT_GATED"]
    rejected = [s.term for s in scores if s.disposition == "REJECTED"]
    signals = detect_conglomerate_triggers(candidate_terms, recipient_names, len(verified))
    conglomerate = target_archetype == "ARCHETYPE_2_CONGLOMERATE" or bool(signals)
    siblings = detect_sibling_terms(target_name, candidate_terms)
    if conglomerate and target_archetype == "ARCHETYPE_2_CONGLOMERATE":
        effective = "ARCHETYPE_2_CONGLOMERATE"
    elif conglomerate:
        effective = "ARCHETYPE_2_MULTI_PRODUCT_LINE"
    else:
        effective = target_archetype
    manifest = TargetManifest(
        target_name=target_name,
        input_hash=evidence_mod.content_hash({
            "target": target_name, "terms": candidate_terms,
            "ueis": sorted(verified), "archetype": target_archetype,
            "nouns": sorted(nouns),
            "window": [from_date, to_date]}),
        window_start=from_date, window_end=to_date,
        standalone_terms=standalone, context_gated_terms=gated,
        rejected_terms=rejected, term_scores=scores,
        positive_regex="|".join(product_terms or []),
        negative_regex="|".join(negative_terms or []),
        verified_oem_ueis=sorted(verified),
        domain_nouns=list(nouns),
        sibling_terms=siblings,
        conglomerate_signals=signals,
        effective_archetype=effective,
        is_conglomerate=conglomerate,
        target_archetype=target_archetype,
        allow_conglomerate_recipient_search=False,
        status="READY" if (standalone or gated) else "NEEDS_USER_INPUT",
    )
    if not standalone and not gated:
        manifest.unresolved_limitations.append(
            "no term reached CONTEXT_GATED precision; user must supply discriminating evidence")
    return {"status": manifest.status, "manifest": manifest,
            "manifest_hash": manifest.canonical_hash(), "continuation": None}
