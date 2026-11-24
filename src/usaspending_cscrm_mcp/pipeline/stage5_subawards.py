"""Stage 5 — segregated subaward ledger (pass-through, never prime obligations).

Verified contracts (port of the proven implementation):
- Per-prime FSRS pull: POST subawards/ {award_id, limit 100} with hasNext
  pagination; per-prime fail-soft.
- Keyword sweep: POST search/spending_by_award/ with subawards:true,
  filters {keywords, award_type_codes:[A,B,C,D]} (codes required even under
  subawards:true — 422 without them), Sub-Award fields, sort
  "Sub-Award Amount" desc, limit 100. Sweep failure is advisory (warning),
  never fatal to the ledger.
- Every sweep hit passes through Stage-4 adjudication; only non-dropped hits
  are kept and rendered with subawardee, amount, prime linkage, matched rule.
- Ledger rows likewise carry their adjudication rule (matched_rule) so
  sibling-product bundles resolve to Tier-1C/bundle treatment, not Tier-1A.

Endpoint-specific adapters (live key spellings are endpoint-specific and MUST
NOT be interchanged):
- PER_PRIME (POST subawards/): snake_case keys — "id", "subaward_number",
  "amount", "description", "recipient_name", "action_date". This endpoint
  returns NO prime-award id and NO agency attribution; those fields stay
  empty on ledger records and the record is flagged accordingly.
- SWEEP (POST search/spending_by_award/ with subawards:true): Title-Case
  display keys — "Sub-Award ID", "Sub-Awardee Name", "Sub-Award Description",
  "Sub-Award Amount", "Sub-Award Date", "Sub-Award Type", "Prime Award ID",
  "prime_award_generated_internal_id", "Prime Recipient Name",
  "Awarding Agency", "Funding Agency" (either agency may be null in source).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from usaspending_cscrm_mcp.client import USAspendingClient
from usaspending_cscrm_mcp.execution import BudgetTracker, RunBudgets
from usaspending_cscrm_mcp.models import AwardRecord, SubawardRecord
from usaspending_cscrm_mcp.pipeline.stage4_adjudicate import adjudicate

logger = logging.getLogger(__name__)


class HydratedSubawardRecord(SubawardRecord):
    """Ledger record with additive agency attribution (never invented).

    Extra fields stay empty when the source endpoint does not return them;
    completeness_defect is True when the source row lacked a usable
    subawardee name, amount, or any agency attribution.
    """

    prime_recipient_name: str = ""
    awarding_agency: str = ""
    funding_agency: str = ""
    completeness_defect: bool = False

    model_config = {"arbitrary_types_allowed": True}

    @property
    def subaward_number(self) -> str:
        """Gate-compatibility alias: AT-06 duck-types on subaward_number."""
        return self.sub_award_id


class PrimeSubawardLedger(BaseModel):
    prime_gid: str
    subaward_count: int = 0
    subawards: list[HydratedSubawardRecord] = Field(default_factory=list)
    error: str | None = None


class SweepHit(BaseModel):
    sub_award_id: str = ""
    description: str = ""
    amount: Decimal | None = None
    prime_award_id: str = ""
    prime_gid: str = ""
    tier: str = "DROPPED"
    rule: str = ""
    snippet: str = ""
    # Additive hydration: populated from the Title-Case sweep row. Empty
    # means the source did not report it (never invented downstream).
    subawardee_name: str = ""
    prime_recipient_name: str = ""
    awarding_agency: str = ""
    funding_agency: str = ""
    completeness_defect: bool = False

    model_config = {"arbitrary_types_allowed": True}

    @property
    def subaward_number(self) -> str:
        """Gate-compatibility alias: AT-06 duck-types on subaward_number."""
        return self.sub_award_id


class Stage5Result(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    status: str = "COMPLETE"
    ledgers: list[PrimeSubawardLedger] = Field(default_factory=list)
    sweep_hits_total: int = 0
    kept_hits: list[SweepHit] = Field(default_factory=list)
    sweep_warning: str | None = None
    incomplete_reason: str | None = None


async def _paginate_subawards(client: USAspendingClient, prime_gid: str,
                             tracker: BudgetTracker) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        if tracker.exhausted():
            return rows, False
        tracker.pages += 1
        resp = await client.post("subawards/",
                                 {"award_id": prime_gid, "limit": 100, "page": page})
        batch = resp.get("results", []) or []
        rows.extend([r for r in batch if isinstance(r, dict)])
        meta = resp.get("page_metadata", {}) or {}
        if not meta.get("hasNext") or not batch:
            return rows, True
        page += 1


def _amount_of(raw: Any) -> Decimal | None:
    try:
        return Decimal(str(raw)) if raw is not None else None
    except Exception:  # noqa: BLE001 - malformed amount stays null
        return None


def _text_of(raw: Any, limit: int = 0) -> str:
    text = "" if raw is None else str(raw)
    return text[:limit] if limit > 0 else text


def _sourced_defect(*, name: str, amount: Decimal | None,
                    awarding: str, funding: str) -> bool:
    """True when the source row lacked name, amount, or any agency signal."""
    return not name or amount is None or not (awarding or funding)


def adapt_per_prime_row(prime_gid: str, row: dict[str, Any]) -> HydratedSubawardRecord:
    """Adapt one POST subawards/ ledger row (snake_case keys only).

    Live keys: "id", "subaward_number", "amount", "description",
    "recipient_name", "action_date". Prime linkage comes from the request
    parameter (the endpoint returns no prime-award id); agency fields are
    always empty on this path (the endpoint returns none).
    """
    name = _text_of(row.get("recipient_name"))
    amount = _amount_of(row.get("amount"))
    return HydratedSubawardRecord(
        sub_award_id=_text_of(row.get("subaward_number") or row.get("id")),
        prime_gid=prime_gid,
        prime_award_id="",
        subawardee_name=name,
        amount=amount,
        description=_text_of(row.get("description"))[:500],
        provenance="fsrs-prime-ledger",
        completeness_defect=_sourced_defect(
            name=name, amount=amount, awarding="", funding=""),
    )


def adapt_sweep_row(h: dict[str, Any]) -> SweepHit:
    """Adapt one subawards:true sweep row (Title-Case display keys only).

    Live keys include "Sub-Award ID", "Sub-Awardee Name",
    "Sub-Award Description", "Sub-Award Amount", "Prime Award ID",
    "prime_award_generated_internal_id", "Prime Recipient Name",
    "Awarding Agency", "Funding Agency" (either agency may be null).
    Adjudication fields (tier/rule/snippet) are filled by the caller.
    """
    name = _text_of(h.get("Sub-Awardee Name"))
    amount = _amount_of(h.get("Sub-Award Amount"))
    awarding = _text_of(h.get("Awarding Agency"))
    funding = _text_of(h.get("Funding Agency"))
    return SweepHit(
        sub_award_id=_text_of(h.get("Sub-Award ID")),
        description=_text_of(h.get("Sub-Award Description"))[:240],
        amount=amount,
        prime_award_id=_text_of(h.get("Prime Award ID")),
        prime_gid=_text_of(h.get("prime_award_generated_internal_id")),
        subawardee_name=name,
        prime_recipient_name=_text_of(h.get("Prime Recipient Name")),
        awarding_agency=awarding,
        funding_agency=funding,
        completeness_defect=_sourced_defect(
            name=name, amount=amount, awarding=awarding, funding=funding),
    )


def _to_record(prime_gid: str, row: dict[str, Any]) -> HydratedSubawardRecord:
    """Backwards-compatible alias for adapt_per_prime_row."""
    return adapt_per_prime_row(prime_gid, row)


async def run_stage5(client: USAspendingClient, prime_gids: list[str], *,
                     sweep_terms: list[str] | None = None,
                     standalone_terms: list[str] | None = None,
                     gated_terms: list[str] | None = None,
                     verified_ueis: set[str] | None = None,
                     homograph_ueis: set[str] | None = None,
                     is_conglomerate: bool = False,
                     sibling_terms: list[str] | None = None,
                     target_name: str = "",
                     budgets: RunBudgets | None = None) -> Stage5Result:
    """Build the segregated subaward ledger and adjudicated sweep hits."""
    budgets = budgets or RunBudgets()
    tracker = BudgetTracker(budgets)
    result = Stage5Result()
    for gid in prime_gids:
        if tracker.exhausted():
            result.status = "INCOMPLETE"
            result.incomplete_reason = tracker.exhausted()
            return result
        try:
            rows, _ = await _paginate_subawards(client, gid, tracker)
            ledger_subs: list[HydratedSubawardRecord] = []
            for r in rows:
                rec = adapt_per_prime_row(gid, r)
                pseudo = AwardRecord(gid=gid, base_description=rec.description)
                verdict = adjudicate(
                    pseudo, standalone_terms=standalone_terms or [],
                    gated_terms=gated_terms or [], verified_ueis=verified_ueis or set(),
                    homograph_ueis=homograph_ueis or set(),
                    is_conglomerate=is_conglomerate,
                    sibling_terms=sibling_terms or [], target_name=target_name)
                rec.matched_rule = verdict.rule
                ledger_subs.append(rec)
            result.ledgers.append(PrimeSubawardLedger(
                prime_gid=gid, subaward_count=len(rows), subawards=ledger_subs))
        except Exception as exc:  # noqa: BLE001 - per-prime fail-soft
            logger.warning("subaward lookup failed for %s: %s", gid, exc)
            result.ledgers.append(PrimeSubawardLedger(prime_gid=gid, error=str(exc)[:200]))
    terms = sweep_terms or []
    if terms:
        hits: list[dict[str, Any]] = []
        try:
            page = 1
            while True:
                if tracker.exhausted():
                    result.sweep_warning = "sweep truncated: " + (tracker.exhausted() or "")
                    break
                tracker.pages += 1
                resp = await client.post(
                    "search/spending_by_award/",
                    {"filters": {"keywords": terms,
                                 "award_type_codes": ["A", "B", "C", "D"]},
                     "subawards": True,
                     "fields": ["Sub-Award ID", "Sub-Awardee Name", "Sub-Award Description",
                                "Sub-Award Amount", "Sub-Award Date", "Sub-Award Type",
                                "Prime Award ID", "prime_award_generated_internal_id",
                                "Prime Recipient Name", "Awarding Agency",
                                "Funding Agency"],
                     "sort": "Sub-Award Amount", "order": "desc",
                     "limit": 100, "page": page})
                batch = resp.get("results", []) or []
                hits.extend([r for r in batch if isinstance(r, dict)])
                meta = resp.get("page_metadata", {}) or {}
                if not meta.get("hasNext") or not batch:
                    break
                page += 1
        except Exception as exc:  # noqa: BLE001 - sweep advisory; ledger stands
            logger.warning("subaward keyword sweep failed: %s", exc)
            result.sweep_warning = f"keyword sweep unavailable: {exc}"
        result.sweep_hits_total = len(hits)
        for h in hits:
            desc = _text_of(h.get("Sub-Award Description"))
            pseudo = AwardRecord(gid=_text_of(h.get("prime_award_generated_internal_id") or ""),
                                 base_description=desc)
            verdict = adjudicate(
                pseudo, standalone_terms=standalone_terms or [],
                gated_terms=gated_terms or [], verified_ueis=verified_ueis or set(),
                homograph_ueis=homograph_ueis or set(),
                is_conglomerate=is_conglomerate,
                sibling_terms=sibling_terms or [], target_name=target_name)
            if verdict.tier.startswith("DROPPED"):
                continue
            hit = adapt_sweep_row(h)
            hit.tier, hit.rule, hit.snippet = verdict.tier, verdict.rule, verdict.snippet
            result.kept_hits.append(hit)
        result.kept_hits.sort(key=lambda r: -(r.amount or Decimal("0")))
    return result
