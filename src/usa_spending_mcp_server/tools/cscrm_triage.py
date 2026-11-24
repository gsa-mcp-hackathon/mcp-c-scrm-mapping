"""Tool 10: triage_and_deconflict_target — Stage 0 initial triage.

Never jump from a raw input string into full extraction. Stage 0 researches
corporate lineage, probes candidate terms against the live API for
over-broadness, and either returns a locked SearchScopeManifest (READY) or a
deconfliction brief with targeted questions (NEEDS_USER_INPUT).
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from usa_spending_mcp_server.client import USASpendingClient
from usa_spending_mcp_server.cscrm_lib import award_count, limited_get, limited_post
from usa_spending_mcp_server.cscrm_systemic import (
    is_high_collision_token,
    sibling_nouns_present,
    strip_term_variants,
    taxonomy_vetoed,
)

logger = logging.getLogger(__name__)

# Generic polysemy traps observed across C-SCRM targets. A probe row matching
# any of these counts against the term's sample precision.
NEGATIVE_POLYSEME_PATTERNS = (
    r"\b\d+\s*°?\s*[cf]\s+cure\b",          # composites curing ("180C cure")
    r"\bcured?\s+(meat|ham|bacon|pork|fish)\b|\b(ham|bacon|pork)\b.*\bcure",
    r"\bsoftware\s+house\s+(international|ltd|limited|inc\.?)\b",  # SHI reseller
    r"\bistar\s+financial\b",
    r"\bintelligence[,\s]+surveillance\b|\btarget\s+acquisition\b",
    r"\belectronic\s+warfare\b",
)
NEGATIVE_RE = re.compile("|".join(f"(?:{p})" for p in NEGATIVE_POLYSEME_PATTERNS),
                         re.IGNORECASE)

POSITIVE_DOMAIN_RE = re.compile(
    r"\b(pacs|iess|electronic\s+security|access\s+control|card\s+reader|"
    r"badge|turnstile|door\s+controller|intrusion|video\s+surveillance|"
    r"c[\s\-_*.•]*cure|cc\s*9000|cure\s+series|istar|software\s+house|alarm|intercom|"
    r"visitor\s+management|surveillance|camera|lenel|genetec|amag|"
    r"hirsch|identiv|monitor\s+dynamics|security)\b",
    re.IGNORECASE)

SAMPLE_FIELDS = ["Award ID", "Recipient Name", "Recipient UEI", "Description",
                 "Award Amount", "Awarding Agency", "NAICS Code", "PSC Code",
                 "generated_internal_id"]

TIER_A_LIMIT = 2500
STANDALONE_THRESHOLD = 0.85
GATED_THRESHOLD = 0.15
CONFIRM_THRESHOLD = 0.50


def expand_variants(term: str) -> list[str]:
    """Orthographic variants: hyphen/space/bullet/dot/star + version splits.

    Drops degenerate variants (single chars, pure numbers) that would probe
    the API with meaningless stems.
    """
    out = [term]
    seps = ["-", " ", "", "\u2022", ".", "*"]
    m = re.match(r"^(.*?)[\s\-_\u2022.*]+(\d+\w*)$", term.strip())
    if m:
        stem, version = m.group(1).strip(), m.group(2).strip()
        stem_nospace = re.sub(r"[\s\-_\u2022.*]+", "", stem)
        for s in seps:
            out.append(f"{stem}{s}{version}".strip())
        out.append(stem)
        out.append(stem_nospace)
        out.append(f"{stem_nospace}{version}")
    else:
        compact = re.sub(r"[\s\-_\u2022.*]+", "", term)
        out.append(compact)
        spaced = re.sub(r"[\-_\u2022.*]+", " ", term).strip()
        if spaced != term:
            out.append(spaced)
    seen: list[str] = []
    for v in (x.strip() for x in out):
        if not v:
            continue
        alnum = re.sub(r"[^A-Za-z0-9]", "", v)
        if len(alnum) < 3 or alnum.isdigit():
            continue
        if v.lower() not in {s.lower() for s in seen}:
            seen.append(v)
    return seen


def _classify_archetype(target_query: str, confirmed: dict[str, Any]) -> str:
    if confirmed.get("target_archetype"):
        return confirmed["target_archetype"]
    if confirmed.get("is_pure_play_vendor") is True:
        return "ARCHETYPE_1_PURE_PLAY_VENDOR"
    if confirmed.get("oem_parent_name"):
        return "ARCHETYPE_2_CONGLOMERATE_PRODUCT_LINE"
    return "ARCHETYPE_2_CONGLOMERATE_PRODUCT_LINE"


def _score_sample(term: str, sample: list[dict[str, Any]],
                  verified_oem_ueis: set[str] | None = None,
                  is_pure_play: bool = False) -> dict[str, Any]:
    """INV1 orthogonal precision over an already-fetched sample (pure).

    The retrieval trigger `term` is stripped first; a row is a true
    positive IFF Signal A (verified UEI + pure-play) or Signal B
    (non-excluded taxonomy + sibling noun outside `term`) survives.
    """
    verified_oem_ueis = verified_oem_ueis or set()
    true_pos = 0
    flagged = []
    for r in sample:
        text = f"{r.get('Description') or ''} {r.get('Recipient Name') or ''}"
        if NEGATIVE_RE.search(text):
            flagged.append(r)
            continue
        stripped = strip_term_variants(text, term)
        signal_a = is_pure_play and (r.get("Recipient UEI") or "") in verified_oem_ueis
        signal_b = (not taxonomy_vetoed(r.get("NAICS Code"), r.get("PSC Code"))
                    and bool(sibling_nouns_present(stripped)))
        if signal_a or signal_b:
            true_pos += 1
        else:
            flagged.append(r)
    recipients: dict[str, int] = {}
    for r in sample:
        recipients[r.get("Recipient Name") or "UNKNOWN"] = \
            recipients.get(r.get("Recipient Name") or "UNKNOWN", 0) + 1
    precision = round(true_pos / len(sample), 3) if sample else None
    multi_entity = len(recipients) >= 4
    collision = is_high_collision_token(term, multi_entity=multi_entity)
    return {"true_positives": true_pos, "flagged_rows": len(flagged),
            "precision": precision, "multi_entity_sample": multi_entity,
            "high_collision_token": collision,
            "flagged_examples": flagged[:5], "top_recipients": recipients}


def _disposition(term: str, total: int, precision: float | None,
                 collision: bool) -> str:
    """INV1 disposition with the high-collision ceiling."""
    if precision is not None and total > TIER_A_LIMIT:
        return "REJECTED_OVERBROAD"
    if precision is None:
        return "NO_HITS"
    if collision:
        return ("REJECTED_OVERBROAD" if (precision or 0) < GATED_THRESHOLD
                else "CONTEXT_GATED")
    if precision >= STANDALONE_THRESHOLD:
        return "STANDALONE_SAFE"
    if precision >= GATED_THRESHOLD:
        return "CONTEXT_GATED"
    return "REJECTED_OVERBROAD"


async def _probe_term(client: USASpendingClient, term: str,
                      start_date: str, end_date: str,
                      verified_oem_ueis: set[str] | None = None,
                      is_pure_play: bool = False) -> dict[str, Any]:
    """Pre-flight count + 25-row sample for one candidate term (INV1 orthogonal)."""
    verified_oem_ueis = verified_oem_ueis or set()
    filters = {"time_period": [{"start_date": start_date, "end_date": end_date}],
               "award_type_codes": ["A", "B", "C", "D"],
               "keywords": [term]}
    try:
        counts = await award_count(client, filters)
        total = sum(counts.values())
    except Exception as exc:  # noqa: BLE001 - probe is advisory
        return {"term": term, "error": str(exc)[:160]}
    sample: list[dict[str, Any]] = []
    if total > 0:
        try:
            resp = await limited_post(client, "search/spending_by_award/",
                                      {"filters": filters, "fields": SAMPLE_FIELDS,
                                       "sort": "Award Amount", "order": "desc",
                                       "limit": 25, "page": 1})
            sample = resp.get("results", []) if isinstance(resp, dict) else []
        except Exception as exc:  # noqa: BLE001 - advisory
            return {"term": term, "total_hits": total,
                    "error": f"sample failed: {exc}"[:160]}
    scored = _score_sample(term, sample, verified_oem_ueis, is_pure_play)
    disposition = _disposition(term, total, scored["precision"], scored["high_collision_token"])
    flagged_rows = scored["flagged_examples"]
    return {"term": term, "total_hits": total, "sample_rows": len(sample),
            "true_positives": scored["true_positives"], "flagged_rows": scored["flagged_rows"],
            "orthogonal_rule": "signal-A-UEI-or-signal-B-taxonomy-plus-sibling",
            "high_collision_token": scored["high_collision_token"],
            "multi_entity_sample": scored["multi_entity_sample"],
            "flagged_examples": [{"award": r.get("Award ID"),
                                  "recipient": r.get("Recipient Name"),
                                  "description": (r.get("Description") or "")[:160]}
                                 for r in flagged_rows
                                 if isinstance(r, dict)][:5] if flagged_rows and isinstance(flagged_rows[0], dict)
            else [{"award": None, "recipient": None,
                   "description": str(r)[:160]} for r in flagged_rows[:5]],
            "top_recipients": sorted(scored["top_recipients"].items(), key=lambda kv: -kv[1])[:5],
            "sample_precision": scored["precision"], "disposition": disposition}


async def triage_and_deconflict_target_core(
    client: USASpendingClient,
    target_query: str,
    candidate_terms: list[str] | None = None,
    start_date: str = "2023-10-01",
    end_date: str = "2026-12-31",
    confirmed_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stage 0: lineage recon + empirical term probes + manifest or brief."""
    confirmed = confirmed_scope or {}
    seeds = candidate_terms or [target_query]
    tier_a: list[str] = []
    for s in seeds:
        tier_a.extend(expand_variants(s))
    tier_a = list(dict.fromkeys(tier_a))[:40]
    short_stems = sorted({t for s in seeds for t in re.split(r"[\s\-_\u2022.*]+", s)
                          if len(t) <= 5 and len(t) >= 2})
    division_names = list(confirmed.get("oem_division_names") or [])
    verified_ueis = {u for u in list(confirmed.get("oem_family_ueis") or []) if u}
    archetype = _classify_archetype(target_query, confirmed)
    is_pure_play = archetype == "ARCHETYPE_1_PURE_PLAY_VENDOR"

    probes = []
    for term in tier_a:
        probes.append(await _probe_term(client, term, start_date, end_date,
                                       verified_oem_ueis=verified_ueis,
                                       is_pure_play=is_pure_play))

    # Corporate lineage: resolve supplied UEIs; harvest child-record UEIs
    # best-effort into the verified family set.
    lineage: dict[str, Any] = {"oem_family_ueis": sorted(verified_ueis),
                               "trees": []}
    for uei in sorted(verified_ueis)[:10]:
        harvested: set[str] = set()
        try:
            children = await limited_get(client, f"recipient/children/{uei}/")
            n_children = 0
            if isinstance(children, list):
                n_children = len(children)
                for record in children:
                    if not isinstance(record, dict):
                        continue
                    for k in ("uei", "recipient_uei", "child_uei"):
                        v = record.get(k)
                        if isinstance(v, str) and v:
                            harvested.add(v)
            verified_ueis |= harvested
            lineage["trees"].append({"uei": uei, "child_count": n_children,
                                     "harvested_child_ueis": sorted(harvested)})
        except Exception as exc:  # noqa: BLE001 - lineage is advisory
            lineage["trees"].append({"uei": uei, "_error": str(exc)[:120]})
    lineage["verified_oem_ueis"] = sorted(verified_ueis)

    standalone = [p["term"] for p in probes if p.get("disposition") == "STANDALONE_SAFE"]
    gated = [p["term"] for p in probes if p.get("disposition") == "CONTEXT_GATED"]
    rejected = [p["term"] for p in probes if p.get("disposition") == "REJECTED_OVERBROAD"]

    triggers: list[str] = []
    core_terms = [target_query, *(candidate_terms or [])]
    core_probes = {p["term"]: p for p in probes}
    if archetype != "ARCHETYPE_1_PURE_PLAY_VENDOR" and not confirmed.get("scope_choice"):
        triggers.append("A_CONGLOMERATE_SCOPE")
    multi_entity = [p for p in probes
                    if len(p.get("top_recipients") or []) >= 2
                    and (p.get("sample_rows") or 0) >= 5]
    if len({r for p in multi_entity for r, _ in (p.get("top_recipients") or [])[:2]}) >= 2:
        triggers.append("B_MULTI_ENTITY_COLLISION")
    low_precision = [t for t in core_terms
                     if (core_probes.get(t, {}).get("sample_precision") or 1.0) < CONFIRM_THRESHOLD]
    if low_precision:
        triggers.append(f"C_OVERBROAD_TERM:{','.join(low_precision)}")

    manifest = {
        "target_name": target_query,
        "target_archetype": archetype,
        "is_pure_play_vendor": archetype == "ARCHETYPE_1_PURE_PLAY_VENDOR",
        "oem_parent_name": confirmed.get("oem_parent_name"),
        "oem_family_ueis": sorted(verified_ueis),
        "verified_oem_ueis": sorted(verified_ueis),
        "excluded_homograph_ueis": sorted({u for u in list(
            confirmed.get("excluded_homograph_ueis") or []) if u}),
        "oem_name_patterns": confirmed.get("oem_name_patterns") or [],
        "oem_division_names": division_names,
        "oem_sibling_product_regex": confirmed.get("oem_sibling_product_regex") or "",
        "standalone_keywords": standalone,
        "context_gated_keywords": gated,
        "rejected_terms": rejected,
        "short_stem_tokens_requiring_compounds": short_stems,
        "positive_domain_context_regex": confirmed.get("positive_domain_context_regex")
        or POSITIVE_DOMAIN_RE.pattern,
        "positive_domain_pscs": confirmed.get("positive_domain_pscs") or [],
        "negative_drop_regex": confirmed.get("negative_drop_regex")
        or NEGATIVE_RE.pattern,
        "competitor_bundle_regex": confirmed.get("competitor_bundle_regex") or "",
        "probe_table": probes,
        "lineage": lineage,
        "artifact": "cscrm_stage0_manifest.json",
    }
    if triggers and not confirmed.get("scope_choice"):
        questions = []
        if any(t.startswith("A_") for t in triggers):
            questions.append("Scope: specific product line only, security division, or whole conglomerate?")
        if any(t.startswith("B_") for t in triggers):
            questions.append("Which of the colliding entities is the true target (UEI/name)?")
        if any(t.startswith("C_") for t in triggers):
            questions.append("Approve compound narrowing / co-occurrence gating for low-precision terms?")
        return {"status": "NEEDS_USER_INPUT", "triggers": triggers,
                "brief": {"probes": probes,
                          "proposed_standalone": standalone,
                          "proposed_gated": gated,
                          "lineage": lineage},
                "questions": questions, "draft_manifest": manifest}
    manifest["scope_choice"] = confirmed.get("scope_choice", "FAST_PATH_UNAMBIGUOUS")
    return {"status": "READY", "manifest": manifest}


def register_triage_and_deconflict_target(mcp: FastMCP, client: USASpendingClient) -> None:
    @mcp.tool(
        name="triage_and_deconflict_target",
        annotations={"title": "Stage 0 triage and identity deconfliction",
                     "readOnlyHint": True, "destructiveHint": False,
                     "idempotentHint": True, "openWorldHint": True},
    )
    async def triage_and_deconflict_target(
        target_query: Annotated[str, Field(description="Raw product/vendor/service string.")],
        candidate_terms: Annotated[list[str] | None, Field(description="Seed terms to expand/probe.")] = None,
        start_date: Annotated[str, Field(description="Window start.")] = "2023-10-01",
        end_date: Annotated[str, Field(description="Window end.")] = "2026-12-31",
        confirmed_scope: Annotated[dict[str, Any] | None, Field(description="User-confirmed scope (scope_choice, UEIs, names, regexes).")] = None,
    ) -> dict[str, Any]:
        """Probe candidate terms, deconflict identity, emit a locked SearchScopeManifest."""
        try:
            return await triage_and_deconflict_target_core(
                client, target_query, candidate_terms, start_date, end_date, confirmed_scope)
        except Exception as exc:  # noqa: BLE001 - structured, fail-closed
            logger.warning("triage_and_deconflict_target failed: %s", exc)
            return {"error": "Stage 0 triage failed."}
