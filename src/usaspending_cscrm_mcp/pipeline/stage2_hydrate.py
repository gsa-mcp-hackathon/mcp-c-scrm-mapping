"""Stage 2 — award hydration & transaction-basis accounting.

Verified contracts (ported from the proven v4/v5 implementation):
- GET awards/{gid}/ — trailing slash mandatory (API 301s slashless GETs and
  this client refuses redirects, so a missing slash is a loud error, not a
  silent redirect).
- POST search/spending_by_transaction — filters {time_period,
  award_type_codes:[A,B,C,D], keywords:[piid]}, limit 100, sort
  "Transaction Amount" desc; rows attributed STRICTLY by generated_internal_id;
  composite dedupe key (gid, mod, action_date, amount).
- POST awards/funding/ {award_id} — assisted-acquisition enrichment when
  awarding/funding toptiers differ (USACE 097/096 civil-works mapping noted).

Unfiltered sums: every deduplicated in-window mod counts (no text filtering).
Ghost rule with non-empty reason enum. Per-award fail-soft with structured
fault entries; budget exhaustion yields INCOMPLETE + continuation.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from usaspending_cscrm_mcp.client import TransportError, USAspendingClient
from usaspending_cscrm_mcp.execution import (
    BudgetTracker,
    ContinuationToken,
    RunBudgets,
    check_scope_binding,
    mint_continuation,
    new_run_id,
)
from usaspending_cscrm_mcp.models import (
    AwardRecord,
    FailedAwardLogEntry,
    TransactionRecord,
    WindowAccounting,
    dedupe_transactions,
)

logger = logging.getLogger(__name__)

TX_FIELDS = [
    "Award ID", "generated_internal_id", "Action Date", "Transaction Amount",
    "Transaction Description", "Award Type", "Action Type", "Recipient Name", "Recipient UEI",
    "Awarding Agency", "Awarding Sub Agency", "Funding Agency", "Funding Sub Agency",
    "Mod", "internal_id",
]

TERMINATION_ACTION_TYPES = frozenset({"C1", "C2", "C3", "C4"})

HISTORICAL_REASONS = (
    "TERMINATED_MOD_C1_C4",
    "ZERO_OR_NEGATIVE_LIFETIME",
    "IN_WINDOW_NET_DEOBLIGATION",
    "ENDED_PRE_WINDOW_ZERO_IN_WINDOW",
)


def _dec_or_none(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def classify_ghost(all_txs: list[TransactionRecord], lifetime: Decimal | None,
                   net_in_window: Decimal, end_raw: str,
                   end_date: str) -> tuple[bool, str | None]:
    """Ghost rule with non-empty reason enum (Decimal-aware port)."""
    terminated = any((t.action_type or "").upper() in TERMINATION_ACTION_TYPES
                     for t in all_txs)
    zero_life = lifetime is not None and lifetime <= Decimal("0.01")
    ended_zero = (net_in_window <= Decimal("0.01") and bool(end_raw)
                  and end_raw < end_date)
    if not (terminated or zero_life or ended_zero):
        return False, None
    if terminated:
        return True, "TERMINATED_MOD_C1_C4"
    if zero_life:
        return True, "ZERO_OR_NEGATIVE_LIFETIME"
    if net_in_window < 0:
        return True, "IN_WINDOW_NET_DEOBLIGATION"
    return True, "ENDED_PRE_WINDOW_ZERO_IN_WINDOW"


def _tx_from_row(gid: str, row: dict[str, Any]) -> TransactionRecord | None:
    amount = _dec_or_none(row.get("Transaction Amount"))
    if amount is None:
        return None
    return TransactionRecord(
        gid=gid, mod=str(row.get("Mod") or ""),
        action_date=str(row.get("Action Date") or "")[:10],
        amount=amount, action_type=str(row.get("Action Type") or ""),
        description=str(row.get("Transaction Description") or ""),
        awarding_agency=str(row.get("Awarding Agency") or "UNKNOWN"),
        funding_agency=str(row.get("Funding Agency") or "UNKNOWN"),
        source_record_id=str(row.get("internal_id") or row.get("Award ID") or ""),
    )


async def _transactions_for_gid(client: USAspendingClient, gid: str, piid: str,
                                start: str, end: str,
                                tracker: BudgetTracker) -> tuple[list[dict[str, Any]], bool]:
    """Transaction pages for one award. Returns (rows, complete)."""
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        if tracker.exhausted():
            return rows, False
        tracker.pages += 1
        resp = await client.post(
            "search/spending_by_transaction/",
            {"filters": {"time_period": [{"start_date": start, "end_date": end}],
                         "award_type_codes": ["A", "B", "C", "D"],
                         "keywords": [piid]},
             "fields": TX_FIELDS, "sort": "Transaction Amount", "order": "desc",
             "limit": 100, "page": page})
        batch = resp.get("results", []) or []
        rows.extend([r for r in batch
                     if isinstance(r, dict) and r.get("generated_internal_id") == gid])
        meta = resp.get("page_metadata", {}) or {}
        if not meta.get("hasNext") or not batch:
            return rows, True
        page += 1


async def _hydrate_one(client: USAspendingClient, gid: str,
                       start_date: str, end_date: str,
                       sem: asyncio.Semaphore,
                       tracker: BudgetTracker) -> AwardRecord | FailedAwardLogEntry:
    async with sem:
        try:
            detail = await client.get(f"awards/{gid}/")
        except Exception as exc:  # noqa: BLE001 - per-award fail-soft
            transient = exc.is_transient if isinstance(exc, TransportError) else False
            return FailedAwardLogEntry(gid=gid, operation="hydrate_detail",
                                       endpoint=f"awards/{gid}/",
                                       error_category=type(exc).__name__,
                                       transient=transient, diagnostic=str(exc)[:300])
        if not isinstance(detail, dict) or not detail.get("piid"):
            return FailedAwardLogEntry(gid=gid, operation="hydrate_detail",
                                       endpoint=f"awards/{gid}/",
                                       error_category="EMPTY_DETAIL",
                                       diagnostic="detail missing piid")
        piid = detail["piid"]
        try:
            raw_rows, tx_complete = await _transactions_for_gid(
                client, gid, piid, start_date, end_date, tracker)
        except Exception as exc:  # noqa: BLE001 - per-award fail-soft
            transient = exc.is_transient if isinstance(exc, TransportError) else False
            return FailedAwardLogEntry(gid=gid, operation="hydrate_transactions",
                                       endpoint="search/spending_by_transaction/",
                                       error_category=type(exc).__name__,
                                       transient=transient, diagnostic=str(exc)[:300])
        parsed = [t for t in (_tx_from_row(gid, r) for r in raw_rows) if t is not None]
        txs = dedupe_transactions(parsed)
        tracker.transactions += len(txs)
        window = WindowAccounting.from_transactions(txs, tx_complete, start_date, end_date)
        lifetime = _dec_or_none(detail.get("total_obligation"))
        pop = detail.get("period_of_performance") or {}
        end_raw = str(pop.get("end_date") or "")[:10]
        ghost, reason = classify_ghost(txs, lifetime, window.window_net_obligations,
                                       end_raw, end_date)
        recipient = detail.get("recipient") or {}
        awarding = detail.get("awarding_agency") or {}
        funding = detail.get("funding_agency") or {}
        a_top = ((awarding.get("toptier_agency") or {}).get("code"))
        f_top = ((funding.get("toptier_agency") or {}).get("code"))
        assisted = None
        if a_top and f_top and a_top != f_top:
            try:
                await client.post("awards/funding/", {"award_id": gid})
                if a_top == "097" and f_top == "096":
                    assisted = "INTERNAL_USACE_CIVIL_WORKS_MAPPING"
                else:
                    assisted = "GENUINE_ASSISTED_ACQUISITION"
            except Exception as exc:  # noqa: BLE001 - enrichment only
                logger.warning("funding enrichment failed for %s: %s", gid, exc)
        naics = detail.get("naics_hierarchy") or {}
        psc = detail.get("psc_hierarchy") or {}
        tx_descs = [t.description for t in txs if t.description]
        if ghost and reason in ("TERMINATED_MOD_C1_C4",):
            lifecycle = "TERMINATED_MOD"
        elif ghost and reason == "ENDED_PRE_WINDOW_ZERO_IN_WINDOW":
            lifecycle = "ENDED_PRE_WINDOW"
        elif end_raw and end_raw >= start_date:
            lifecycle = "CLOSED_WITH_IN_WINDOW_ACTIVITY" if end_raw <= end_date else "ACTIVE"
        else:
            lifecycle = "ACTIVE" if window.window_transaction_count else "UNKNOWN"
        tracker.awards += 1
        return AwardRecord(
            gid=gid, piid=piid,
            parent_gid=(detail.get("parent_award") or {}).get("generated_unique_award_id"),
            parent_piid=(detail.get("parent_award") or {}).get("piid"),
            recipient_name=str(recipient.get("recipient_name") or ""),
            recipient_uei=str(recipient.get("recipient_uei") or ""),
            parent_recipient_uei=str(recipient.get("parent_recipient_uei") or ""),
            awarding_agency=str((awarding.get("toptier_agency") or {}).get("name")
                                or awarding.get("name") or "UNKNOWN"),
            funding_agency=str((funding.get("toptier_agency") or {}).get("name")
                               or funding.get("name") or "UNKNOWN"),
            naics_code=str(((naics.get("base_code") or {}).get("code")) or ""),
            psc_code=str(((psc.get("base_code") or {}).get("code")) or ""),
            base_description=str(detail.get("description") or ""),
            transaction_descriptions=tx_descs,
            start_date=str(pop.get("start_date") or "")[:10],
            end_date=end_raw,
            lifetime_obligated=lifetime, window=window,
            hydration_complete=tx_complete,
            ghost_historical=ghost, historical_reason=reason,
            assisted_classification=assisted,
            award_lifecycle_status=lifecycle,  # type: ignore[arg-type]
        )


async def hydrate_awards(client: USAspendingClient, gids: list[str],
                         start_date: str, end_date: str,
                         budgets: RunBudgets | None = None,
                         signing_key: bytes | None = None,
                         continuation: str | None = None,
                         caller_id: str = "local"
                         ) -> dict[str, Any]:
    """Hydrate awards with fail-soft isolation and resumable progress."""
    budgets = budgets or RunBudgets()
    tracker = BudgetTracker(budgets)
    run_id = new_run_id()
    start_idx = 0
    if continuation and signing_key:
        ct = ContinuationToken.decode(continuation, signing_key)
        check_scope_binding(ct, caller_id, "stage2:window", "hydrate")
        run_id, start_idx = ct.run_id, int(ct.cursor.get("gid_index", 0))
    sem = asyncio.Semaphore(min(budgets.max_concurrency, 8))
    hydrated: list[AwardRecord] = []
    failed: list[FailedAwardLogEntry] = []
    for idx in range(start_idx, len(gids)):
        if tracker.exhausted():
            cont = (mint_continuation(run_id, caller_id, "stage2:window",
                                      "hydrate", {"gid_index": idx}, signing_key)
                    if signing_key else None)
            return {"status": "INCOMPLETE", "reason": tracker.exhausted(),
                    "hydrated": hydrated, "failed": failed,
                    "continuation": cont, "run_id": run_id}
        res = await _hydrate_one(client, gids[idx], start_date, end_date, sem, tracker)
        if isinstance(res, AwardRecord):
            hydrated.append(res)
        else:
            failed.append(res)
    return {"status": "COMPLETE", "hydrated": hydrated, "failed": failed,
            "continuation": None, "run_id": run_id}
