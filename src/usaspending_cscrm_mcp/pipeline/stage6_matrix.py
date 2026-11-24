"""Stage 6 — cohort-aligned agency matrix + completeness reporting.

Matrix invariants (§9):
- One row per (agency, tier) cohort present in the adjudicated set; counts are
  |G(A,T)| over hydrated GIDs — phantom rows are impossible (a row exists iff
  at least one proof GID backs it).
- Assisted acquisitions credit the FUNDING agency; the servicing (awarding)
  agency appears in the servicing ledger, never double-counted.
- Ghost/historical records display as HISTORICAL with their tier preserved.
- Window sums are Decimal-exact; missing transaction data is reported as
  unknown activity, never zero.
- Subaward exposure is segregated (see build_subaward_cohort_rows): prime and
  subaward dollars are NEVER summed — subaward rows never enter prime rows,
  tier totals, or the matrix hash.

Completeness: every queried population carries reported-vs-fetched parity;
failures are structured entries; unresolved limitations are explicit.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from usaspending_cscrm_mcp.evidence import content_hash
from usaspending_cscrm_mcp.models import (
    AwardRecord,
    FailedAwardLogEntry,
    QueryObservation,
)
from usaspending_cscrm_mcp.pipeline.stage4_adjudicate import Adjudication

MAX_PROOF_GIDS = 25

SUBAWARD_DISPLAY = "Tier-1A Subaward"
UNRESOLVED_AGENCY = "Unresolved"


class MatrixRow(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    agency: str
    agency_role: str = "funding"
    tier: str = "Tier-1A"
    display: str = "Tier-1A"
    award_count: int = 0
    window_net: Decimal = Decimal("0")
    window_positive: Decimal = Decimal("0")
    window_negative: Decimal = Decimal("0")
    lifetime_total: Decimal = Decimal("0")
    proof_gids: list[str] = Field(default_factory=list)
    proof_truncated: bool = False


class ServicingEntry(BaseModel):
    servicing_agency: str
    funding_agency: str
    award_count: int = 0
    proof_gids: list[str] = Field(default_factory=list)


class SubawardCohortRow(MatrixRow):
    """Segregated subaward-exposure cohort row.

    Additive CohortRow support for section 1: one row per agency cohort over
    subaward hits, display always "Tier-1A Subaward". Prime and subaward
    dollars are NEVER summed — these rows are excluded from tier totals and
    the matrix hash by construction (see build_matrix).
    """

    cohort: str = "subaward"
    source_note: str = ""


class CompletenessReport(BaseModel):
    populations_queried: int = 0
    populations_complete: int = 0
    awards_hydrated: int = 0
    awards_failed: int = 0
    failed_awards: list[FailedAwardLogEntry] = Field(default_factory=list)
    parity_violations: list[str] = Field(default_factory=list)
    unresolved_limitations: list[str] = Field(default_factory=list)


class Stage6Result(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    matrix: list[MatrixRow] = Field(default_factory=list)
    servicing_ledger: list[ServicingEntry] = Field(default_factory=list)
    subaward_cohorts: list[SubawardCohortRow] = Field(default_factory=list)
    completeness: CompletenessReport = Field(default_factory=CompletenessReport)
    totals: dict[str, str] = Field(default_factory=dict)
    matrix_hash: str = ""


def _sub_field(item: Any, name: str) -> str:
    value = item.get(name) if isinstance(item, dict) else getattr(item, name, None)
    return "" if value is None else str(value)


def _sub_amount(item: Any) -> Decimal:
    value = item.get("amount") if isinstance(item, dict) else getattr(item, "amount", None)
    try:
        return Decimal(str(value)) if value is not None else Decimal("0")
    except Exception:  # noqa: BLE001 - malformed subaward amount counts as zero
        return Decimal("0")


def resolve_subaward_agency(*, awarding: str, funding: str) -> tuple[str, str]:
    """Fallback chain: awarding -> funding -> Unresolved, with source note.

    Returns (agency, note); note is empty when no fallback was needed and
    otherwise records which source field was null.
    """
    awarding, funding = awarding.strip(), funding.strip()
    if awarding:
        if funding:
            return awarding, ""
        return (awarding, "funding agency not reported by source; "
                "cohort credited to awarding agency")
    if funding:
        return (funding, "awarding agency not reported by source; "
                "cohort credited to funding agency")
    return (UNRESOLVED_AGENCY, "awarding and funding agencies not reported "
            "by source; grouped as Unresolved")


def build_subaward_cohort_rows(subawards: Sequence[Any]) -> list[SubawardCohortRow]:
    """Emit one segregated cohort row per agency over subaward hits.

    Accepts SweepHit / HydratedSubawardRecord objects or their JSON dicts.
    Decimal math is balanced (window_net == window_positive + window_negative
    with window_negative pinned at zero), every row has award_count >= 1 and
    a non-empty proof list, so existing AT-08 invariants hold. Prime and
    subaward dollars are NEVER summed here or downstream: callers must not
    merge these amounts into prime rows or tier totals.
    """
    cohorts: dict[str, SubawardCohortRow] = {}
    notes: dict[str, str] = {}
    for index, item in enumerate(subawards):
        agency, note = resolve_subaward_agency(
            awarding=_sub_field(item, "awarding_agency"),
            funding=_sub_field(item, "funding_agency"))
        row = cohorts.get(agency)
        if row is None:
            row = cohorts[agency] = SubawardCohortRow(
                agency=agency, tier="Tier-1A", display=SUBAWARD_DISPLAY)
            if note:
                notes[agency] = note
        amount = _sub_amount(item)
        row.award_count += 1
        row.window_positive += amount
        row.window_net += amount
        # window_negative stays Decimal("0"): net == positive + negative.
        row.lifetime_total += amount
        prime_gid = _sub_field(item, "prime_gid").strip()
        sub_id = _sub_field(item, "sub_award_id").strip()
        proof = prime_gid or (f"subaward:{sub_id}" if sub_id else f"subaward-unresolved-{index}")
        if len(row.proof_gids) < MAX_PROOF_GIDS:
            row.proof_gids.append(proof)
        else:
            row.proof_truncated = True
    rows = sorted(cohorts.values(), key=lambda r: r.agency)
    for row in rows:
        if row.agency in notes:
            row.source_note = notes[row.agency]
    return rows


def build_matrix(records: list[AwardRecord],
                 adjudications: dict[str, Adjudication],
                 agency_of: dict[str, str] | None = None,
                 subawards: Sequence[Any] | None = None) -> Stage6Result:
    """Assemble the cohort matrix. agency_of overrides funding agency per GID
    (assisted-acquisition credit); servicing agencies go to the ledger.

    subawards (optional SweepHit / record / dict items) emits segregated
    subaward cohort rows. Prime and subaward dollars are NEVER summed:
    subaward amounts appear ONLY in subaward_cohorts — totals and the matrix
    hash cover prime rows exclusively.
    """
    agency_of = agency_of or {}
    cohorts: dict[tuple[str, str, str], MatrixRow] = {}
    servicing: dict[tuple[str, str], ServicingEntry] = {}
    for rec in records:
        adj = adjudications.get(rec.gid)
        if adj is None or adj.tier.startswith("DROPPED"):
            continue
        funder = agency_of.get(rec.gid, rec.funding_agency or "UNKNOWN")
        key = (funder, adj.tier, adj.display)
        row = cohorts.get(key)
        if row is None:
            row = cohorts[key] = MatrixRow(agency=funder, tier=adj.tier,
                                           display=adj.display)
        row.award_count += 1
        row.window_net += rec.window.window_net_obligations
        row.window_positive += rec.window.window_positive_obligations
        row.window_negative += rec.window.window_negative_deobligations
        if rec.lifetime_obligated is not None:
            row.lifetime_total += rec.lifetime_obligated
        if len(row.proof_gids) < MAX_PROOF_GIDS:
            row.proof_gids.append(rec.gid)
        else:
            row.proof_truncated = True
        if rec.assisted_classification == "GENUINE_ASSISTED_ACQUISITION":
            skey = (rec.awarding_agency or "UNKNOWN", funder)
            entry = servicing.get(skey)
            if entry is None:
                entry = servicing[skey] = ServicingEntry(
                    servicing_agency=skey[0], funding_agency=skey[1])
            entry.award_count += 1
            entry.proof_gids.append(rec.gid)
    matrix = sorted(cohorts.values(), key=lambda r: (r.agency, r.tier, r.display))
    tier_totals: dict[str, Decimal] = {}
    for row in matrix:
        tier_totals[row.display] = tier_totals.get(row.display, Decimal("0")) + row.window_net
    result = Stage6Result(
        matrix=matrix, servicing_ledger=sorted(
            servicing.values(), key=lambda e: (e.funding_agency, e.servicing_agency)),
        totals={k: str(v) for k, v in sorted(tier_totals.items())})
    if subawards is not None:
        # Segregated: never merged into matrix, totals, or the hash.
        result.subaward_cohorts = build_subaward_cohort_rows(subawards)
    result.matrix_hash = content_hash([r.model_dump(mode="json") for r in matrix])
    return result


def check_completeness(observations: list[QueryObservation],
                       hydrated: int, failed: list[FailedAwardLogEntry],
                       limitations: list[str] | None = None) -> CompletenessReport:
    """Parity audit: reported-vs-fetched per population; violations explicit."""
    report = CompletenessReport(
        populations_queried=len(observations), awards_hydrated=hydrated,
        awards_failed=len(failed), failed_awards=failed,
        unresolved_limitations=list(limitations or []))
    for obs in observations:
        if not obs.complete:
            report.parity_violations.append(
                f"{obs.query_id}: incomplete (reported={obs.reported_count} "
                f"fetched={obs.fetched_count})")
        elif obs.reported_count >= 0 and obs.reported_count != obs.fetched_count:
            report.parity_violations.append(
                f"{obs.query_id}: count mismatch (reported={obs.reported_count} "
                f"fetched={obs.fetched_count})")
        else:
            report.populations_complete += 1
    return report
