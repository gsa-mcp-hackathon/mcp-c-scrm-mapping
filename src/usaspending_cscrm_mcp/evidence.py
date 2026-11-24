"""Typed evidence + canonical hashing (§9).

Every adjudicated record carries a rule_id and rule-appropriate evidence.
A content hash provides integrity detection, not proof of correctness.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

EvidenceKind = Literal[
    "lexical",
    "recipient_identity",
    "parent_link",
    "transaction_derived",
    "count_reconciliation",
    "policy_decision",
]


class Evidence(BaseModel):
    """One typed evidence item."""

    kind: EvidenceKind
    rule_id: str = Field(description="Rule identifier, e.g. KEEP_MANIFEST_PRODUCT_MATCH.")
    # lexical
    exact_substring: str = ""
    field_path: str = ""
    source_reference: str = ""
    substring_verified: bool = False
    # recipient_identity
    verified_identity: str = ""
    identity_association: str = ""
    # parent_link
    parent_gid: str = ""
    parent_policy: str = ""
    parent_policy_evidence_ref: str = ""
    # transaction_derived
    window_start: str = ""
    window_end: str = ""
    arithmetic_inputs: dict[str, str] = Field(default_factory=dict)
    derived_amount: str = ""
    # count_reconciliation
    expected_count: int | None = None
    observed_count: int | None = None
    population_definition: str = ""
    # policy_decision
    rationale: str = ""


def canonical_json(payload: Any) -> str:
    """Deterministic serialization: sorted keys, no whitespace, UTF-8."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def content_hash(payload: Any) -> str:
    """SHA-256 over the canonical serialization (integrity detection)."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def verify_lexical_evidence(corpus: str, evidence: Evidence) -> bool:
    """A lexical snippet must match the referenced source substring exactly."""
    if evidence.kind != "lexical" or not evidence.exact_substring:
        return False
    return evidence.exact_substring in (corpus or "")
