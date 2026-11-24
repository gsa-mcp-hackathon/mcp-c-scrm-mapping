"""Execution model A — bounded resumable runs (§6, Model A).

- No durable jobs: state lives in integrity-protected continuation tokens.
- A continuation binds: run id, caller id, manifest hash, query hash, progress
  cursor, expiry, and state version. Tokens are HMAC-SHA256 signed; payload
  mutation invalidates the signature.
- Retries are idempotent (dedupe by canonical award key); restart replays
  from the cursor without double counting.
- Exhaustion (budgets, cancellation, restart loss, required-data failure)
  yields INCOMPLETE, never PASS.
- Restart across processes without approved operational storage is
  unsupported: cursors survive only via returned tokens, not server memory.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field


class RunBudgets(BaseModel):
    """Required limits (§6). Enforced before/during collection."""

    per_request_timeout_s: float = 60.0
    overall_deadline_s: float = 1500.0
    max_pages: int = 200
    max_awards: int = 5000
    max_transactions: int = 50000
    max_relationships: int = 10000
    max_output_bytes: int = 2_000_000
    max_concurrency: int = 8
    max_retries_per_operation: int = 3


STATE_VERSION = 1
CONTINUATION_TTL_S = 3600


def _b64url(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    import base64
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class ContinuationToken(BaseModel):
    """Integrity-protected, expiring run cursor."""

    run_id: str
    caller_id: str
    manifest_hash: str
    query_hash: str
    cursor: dict[str, Any] = Field(default_factory=dict)
    created_at: int = 0
    expires_at: int = 0
    state_version: int = STATE_VERSION

    def encode(self, signing_key: bytes) -> str:
        body = _b64url(json.dumps(self.model_dump(mode="json"),
                                  sort_keys=True, separators=(",", ":")).encode())
        sig = hmac.new(signing_key, body.encode(), hashlib.sha256).digest()
        return body + "." + _b64url(sig)

    @classmethod
    def decode(cls, token: str, signing_key: bytes,
               now: int | None = None) -> ContinuationToken:
        try:
            body, sig = token.rsplit(".", 1)
        except ValueError as exc:
            raise ValueError("malformed continuation token") from exc
        expected = hmac.new(signing_key, body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url(expected), sig):
            raise ValueError("continuation signature invalid (payload mutated?)")
        payload = json.loads(_b64url_decode(body))
        ct = cls(**payload)
        if ct.state_version != STATE_VERSION:
            raise ValueError(f"unsupported state version {ct.state_version}")
        if (now if now is not None else int(time.time())) > ct.expires_at:
            raise ValueError("continuation token expired")
        return ct


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:16]


def mint_continuation(run_id: str, caller_id: str, manifest_hash: str,
                      query_hash: str, cursor: dict[str, Any],
                      signing_key: bytes,
                      ttl_s: int = CONTINUATION_TTL_S) -> str:
    now = int(time.time())
    return ContinuationToken(run_id=run_id, caller_id=caller_id,
                             manifest_hash=manifest_hash,
                             query_hash=query_hash, cursor=cursor,
                             created_at=now, expires_at=now + ttl_s).encode(signing_key)


def check_scope_binding(token: ContinuationToken, caller_id: str,
                        manifest_hash: str, query_hash: str) -> None:
    """Continuation is bound to caller + manifest + query. Mismatch aborts."""
    if token.caller_id != caller_id:
        raise ValueError("continuation caller mismatch")
    if token.manifest_hash != manifest_hash:
        raise ValueError("continuation manifest mismatch")
    if token.query_hash != query_hash:
        raise ValueError("continuation query mismatch")


class BudgetTracker:
    """Counts consumption against RunBudgets; exhaustion -> INCOMPLETE."""

    def __init__(self, budgets: RunBudgets) -> None:
        self.budgets = budgets
        self.pages = 0
        self.awards = 0
        self.transactions = 0
        self.relationships = 0
        self.output_bytes = 0
        self.started = time.monotonic()

    def exhausted(self) -> str | None:
        b = self.budgets
        if self.pages >= b.max_pages:
            return "max_pages"
        if self.awards >= b.max_awards:
            return "max_awards"
        if self.transactions >= b.max_transactions:
            return "max_transactions"
        if self.relationships >= b.max_relationships:
            return "max_relationships"
        if self.output_bytes >= b.max_output_bytes:
            return "max_output_bytes"
        if time.monotonic() - self.started >= b.overall_deadline_s:
            return "overall_deadline"
        return None
