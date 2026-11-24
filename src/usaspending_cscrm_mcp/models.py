"""Canonical data models (Pydantic v2, Decimal money, GID primary keys).

Identity rules (§9):
- Awards join/dedupe exclusively on `generated_internal_id` (GID). Bare PIID
  joins are prohibited (verified CDC 75D30124F00014 collision).
- Modification numbers are NOT globally unique: transaction identity is the
  composite (gid, mod, action_date, amount).
- Subaward identity is the verified source subaward id scoped to its prime.
- Missing transaction data is unknown activity, never zero.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ActivityStatus = Literal[
    "NO_TRANSACTIONS",
    "POSITIVE_ONLY",
    "NEGATIVE_ONLY",
    "MIXED_NONZERO_NET",
    "OFFSETTING_ZERO_NET",
    "ZERO_DOLLAR_ONLY",
    "UNKNOWN_INCOMPLETE_RETRIEVAL",
]

AwardLifecycleStatus = Literal[
    "ACTIVE",
    "CLOSED_WITH_IN_WINDOW_ACTIVITY",
    "ENDED_PRE_WINDOW",
    "TERMINATED_MOD",
    "UNKNOWN",
]

EvidenceTier = Literal["Tier-1A", "Tier-1B", "Tier-2", "Tier-3", "DROPPED"]
DisplayCategory = Literal["Tier-1A", "Tier-1B", "Tier-2", "Tier-3", "DROPPED", "HISTORICAL"]

MonetaryBasis = Literal["vendor_associated", "product_associated", "product_allocated"]


def _dec(v: object) -> Decimal:
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


class TransactionRecord(BaseModel):
    """One verified USAspending transaction row (deduplicated)."""

    model_config = {"arbitrary_types_allowed": True}

    gid: str = Field(description="Canonical award key (generated_internal_id).")
    mod: str = Field(default="", description="Modification number (not globally unique).")
    action_date: str = Field(description="YYYY-MM-DD action date.")
    amount: Decimal = Field(description="Signed transaction obligation.")
    action_type: str = Field(default="")
    description: str = Field(default="")
    awarding_agency: str = Field(default="UNKNOWN")
    funding_agency: str = Field(default="UNKNOWN")
    source_record_id: str = Field(default="", description="Verified source identifier.")

    _norm_amount = field_validator("amount", mode="before")(classmethod(lambda cls, v: _dec(v)))


class WindowAccounting(BaseModel):
    """Window sums for one award. Negative deobligations stay signed so that
    net = positive + negative. Missing data is never converted to zero."""

    model_config = {"arbitrary_types_allowed": True}

    window_positive_obligations: Decimal = Decimal("0")
    window_negative_deobligations: Decimal = Decimal("0")
    window_net_obligations: Decimal = Decimal("0")
    window_transaction_count: int = 0
    window_activity_status: ActivityStatus = "UNKNOWN_INCOMPLETE_RETRIEVAL"
    transaction_population_complete: bool = False

    _norm1 = field_validator("window_positive_obligations", mode="before")(
        classmethod(lambda cls, v: _dec(v)))
    _norm2 = field_validator("window_negative_deobligations", mode="before")(
        classmethod(lambda cls, v: _dec(v)))
    _norm3 = field_validator("window_net_obligations", mode="before")(
        classmethod(lambda cls, v: _dec(v)))

    @classmethod
    def from_transactions(cls, txs: list[TransactionRecord],
                          complete: bool,
                          start_date: str = "",
                          end_date: str = "9999-12-31") -> WindowAccounting:
        windowed = [t for t in txs if start_date <= t.action_date[:10] <= end_date]
        pos = sum((t.amount for t in windowed if t.amount > 0), Decimal("0"))
        neg = sum((t.amount for t in windowed if t.amount < 0), Decimal("0"))
        n = len(windowed)
        if not complete:
            status: ActivityStatus = "UNKNOWN_INCOMPLETE_RETRIEVAL"
        elif n == 0:
            status = "NO_TRANSACTIONS"
        elif all(t.amount == 0 for t in txs):
            status = "ZERO_DOLLAR_ONLY"
        elif pos > 0 and neg == 0:
            status = "POSITIVE_ONLY"
        elif pos == 0 and neg < 0:
            status = "NEGATIVE_ONLY"
        elif pos + neg == 0:
            status = "OFFSETTING_ZERO_NET"
        else:
            status = "MIXED_NONZERO_NET"
        return cls(window_positive_obligations=pos,
                   window_negative_deobligations=neg,
                   window_net_obligations=pos + neg,
                   window_transaction_count=n,
                   window_activity_status=status,
                   transaction_population_complete=complete)


class AwardRecord(BaseModel):
    """Canonical hydrated award."""

    model_config = {"arbitrary_types_allowed": True}

    gid: str = Field(description="Primary key: generated_internal_id.")
    piid: str = Field(default="")
    parent_gid: str | None = None
    parent_piid: str | None = None
    recipient_name: str = ""
    recipient_uei: str = ""
    parent_recipient_uei: str = ""
    awarding_agency: str = "UNKNOWN"
    funding_agency: str = "UNKNOWN"
    naics_code: str = ""
    psc_code: str = ""
    base_description: str = ""
    transaction_descriptions: list[str] = Field(default_factory=list)
    start_date: str = ""
    end_date: str = ""
    lifetime_obligated: Decimal | None = None
    window: WindowAccounting = Field(default_factory=WindowAccounting)
    hydration_complete: bool = False
    ghost_historical: bool = False
    historical_reason: str | None = None
    assisted_classification: str | None = None
    evidence_tier: EvidenceTier = "DROPPED"
    display_category: DisplayCategory = "DROPPED"
    award_lifecycle_status: AwardLifecycleStatus = "UNKNOWN"
    monetary_basis: MonetaryBasis = "vendor_associated"

    _norm_life = field_validator("lifetime_obligated", mode="before")(
        classmethod(lambda cls, v: None if v is None else _dec(v)))

def transaction_key(tx: TransactionRecord) -> tuple[str, str, str, str]:
    """Composite transaction identity (mod numbers are not globally unique)."""
    return (tx.gid, tx.mod, tx.action_date, str(tx.amount))


def dedupe_awards_by_gid(records: list[AwardRecord]) -> list[AwardRecord]:
    """Deduplicate the award ledger exclusively by GID (never bare PIID)."""
    seen: dict[str, AwardRecord] = {}
    for r in records:
        seen.setdefault(r.gid, r)
    return list(seen.values())


def dedupe_transactions(txs: list[TransactionRecord]) -> list[TransactionRecord]:
    seen: dict[tuple[str, str, str, str], TransactionRecord] = {}
    for t in txs:
        seen.setdefault(transaction_key(t), t)
    return list(seen.values())


class SubawardRecord(BaseModel):
    """Pass-through FSRS subaward (never added to prime obligations)."""

    model_config = {"arbitrary_types_allowed": True}

    sub_award_id: str
    prime_gid: str = ""
    prime_award_id: str = ""
    subawardee_name: str = ""
    amount: Decimal | None = None
    description: str = ""
    reporting_period: str = ""
    matched_rule: str = ""
    provenance: str = ""

    _norm_amt = field_validator("amount", mode="before")(
        classmethod(lambda cls, v: None if v is None else _dec(v)))


class RelationshipEdge(BaseModel):
    """One verified parent-child IDV edge (immediate children only)."""

    parent_gid: str
    child_gid: str
    depth: int = 1


class QueryObservation(BaseModel):
    """One executed query population (parity evidence)."""

    query_id: str
    endpoint: str
    canonical_filter_hash: str
    reported_count: int
    fetched_count: int
    page_size: int
    terminal_has_next: bool
    retrieved_at: str = ""
    complete: bool = False


class FailedAwardLogEntry(BaseModel):
    """Structured per-award failure (fault isolation, §7)."""

    gid: str
    operation: str
    endpoint: str
    error_category: str
    transient: bool = False
    retry_count: int = 0
    diagnostic: str = ""
    recorded_at: str = ""

    @staticmethod
    def utcnow_iso() -> str:
        return datetime.now(UTC).isoformat()
