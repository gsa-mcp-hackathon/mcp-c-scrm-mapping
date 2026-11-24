"""Phase 2 unit tests: models, evidence, transport, execution, startup."""

from decimal import Decimal

import httpx
import pytest

from usaspending_cscrm_mcp import evidence as evidence_mod
from usaspending_cscrm_mcp import models as models_mod
from usaspending_cscrm_mcp.client import (
    NonTransientError,
    TokenBucket,
    TransportError,
    USAspendingClient,
    _is_transient_exc,
    parse_retry_after_seconds,
    require_trailing_slash,
)
from usaspending_cscrm_mcp.execution import (
    BudgetTracker,
    ContinuationToken,
    RunBudgets,
    check_scope_binding,
    mint_continuation,
)


def tx(gid="G1", amount=100, date="2024-03-01", mod="0", desc="Software License"):
    return models_mod.TransactionRecord(gid=gid, action_date=date, amount=amount,
                               mod=mod, description=desc)


# ---- Decimal accounting ----------------------------------------------------

def test_decimal_exact_and_signed():
    w = models_mod.WindowAccounting.from_transactions(
        [tx(amount=100), tx(amount=-40, desc="ADMIN DEOBLIGATION")], True)
    assert w.window_positive_obligations == Decimal("100")
    assert w.window_negative_deobligations == Decimal("-40")
    assert w.window_net_obligations == Decimal("60")
    assert w.window_activity_status == "MIXED_NONZERO_NET"


def test_offsetting_zero_net_is_not_zero_activity():
    w = models_mod.WindowAccounting.from_transactions([tx(amount=50), tx(amount=-50)], True)
    assert w.window_net_obligations == Decimal("0")
    assert w.window_activity_status == "OFFSETTING_ZERO_NET"


def test_missing_data_is_unknown_never_zero():
    w = models_mod.WindowAccounting.from_transactions([tx(amount=50)], False)
    assert w.window_activity_status == "UNKNOWN_INCOMPLETE_RETRIEVAL"
    w2 = models_mod.WindowAccounting.from_transactions([], True)
    assert w2.window_activity_status == "NO_TRANSACTIONS"
    w3 = models_mod.WindowAccounting.from_transactions(
        [models_mod.TransactionRecord(gid="G1", action_date="2024-01-01", amount=0)], True)
    assert w3.window_activity_status == "ZERO_DOLLAR_ONLY"


def test_lifecycle_unknown_by_default_not_inferred_dead():
    h = models_mod.AwardRecord(gid="G9")
    assert h.award_lifecycle_status == "UNKNOWN"
    assert h.monetary_basis == "vendor_associated"


# ---- canonical identity ----------------------------------------------------

def test_gid_dedupe_and_piid_collision_survives():
    a = models_mod.AwardRecord(gid="G-JCI", piid="75D30124F00014")
    b = models_mod.AwardRecord(gid="G-ICE", piid="75D30124F00014")
    out = models_mod.dedupe_awards_by_gid([a, b, a])
    assert len(out) == 2
    assert {r.gid for r in out} == {"G-JCI", "G-ICE"}


def test_transaction_composite_key_not_mod_alone():
    t1 = tx(gid="G1", mod="P00001", amount=100)
    t2 = tx(gid="G2", mod="P00001", amount=100)
    assert models_mod.transaction_key(t1) != models_mod.transaction_key(t2)
    assert len(models_mod.dedupe_transactions([t1, t2, t1])) == 2


# ---- evidence ---------------------------------------------------------------

def test_canonical_hash_stable_and_tamper_evident():
    payload = {"b": 1, "a": [1, 2]}
    assert evidence_mod.content_hash(payload) == evidence_mod.content_hash({"a": [1, 2], "b": 1})
    mutated = {"a": [1, 2], "b": 2}
    assert evidence_mod.content_hash(payload) != evidence_mod.content_hash(mutated)


def test_lexical_evidence_requires_exact_substring():
    ev = evidence_mod.Evidence(kind="lexical", rule_id="R1", exact_substring="C-CURE 9000")
    assert evidence_mod.verify_lexical_evidence("INSTALL C-CURE 9000 PANELS", ev) is True
    assert evidence_mod.verify_lexical_evidence("install cure panels", ev) is False
    assert evidence_mod.verify_lexical_evidence("anything", evidence_mod.Evidence(kind="lexical", rule_id="R1")) is False


# ---- transport ----------------------------------------------------------------

def test_slash_guard_rejects_bare_idv_posts():
    with pytest.raises(ValueError):
        require_trailing_slash("idvs/awards")
    with pytest.raises(ValueError):
        require_trailing_slash("idvs/activity")
    require_trailing_slash("idvs/awards/")
    require_trailing_slash("search/spending_by_award/")


def test_retry_after_parsing():
    assert parse_retry_after_seconds("120") == 120.0
    assert parse_retry_after_seconds(None) is None
    assert parse_retry_after_seconds("not-a-number") is None


def test_transient_classification_and_survival_through_wrapping():
    assert _is_transient_exc(httpx.TimeoutException("t")) is True
    assert _is_transient_exc(httpx.RemoteProtocolError("r")) is True
    assert _is_transient_exc(httpx.ConnectError("c")) is True
    assert _is_transient_exc(RuntimeError("plain")) is False
    inner = TransportError("boom", transient=True, endpoint="x", attempts=2)
    outer = TransportError(f"wrapped: {inner}", transient=inner.is_transient,
                           endpoint=inner.endpoint, attempts=inner.attempts + 1)
    assert outer.is_transient is True and outer.attempts == 3
    assert NonTransientError("nope").is_transient is False


def test_allowlist_rejects_off_domain_urls():
    c = USAspendingClient()
    with pytest.raises(NonTransientError):
        c._url("https://evil.example.com/x")
    assert c._url("search/spending_by_award/").startswith("https://api.usaspending.gov/api/v2/")


@pytest.mark.asyncio
async def test_retry_on_429_then_success_counts_attempts():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    c = USAspendingClient()
    c.client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="https://api.usaspending.gov")
    out = await c.post("search/spending_by_award/", {"filters": {}})
    assert out == {"ok": True}
    assert calls["n"] == 3
    await c.aclose()


@pytest.mark.asyncio
async def test_404_fails_fast_first_attempt():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, json={"detail": "nope"})

    c = USAspendingClient()
    c.client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="https://api.usaspending.gov")
    with pytest.raises(NonTransientError):
        await c.post("awards/NOPE/", {})
    assert calls["n"] == 1
    await c.aclose()


@pytest.mark.asyncio
async def test_redirects_never_followed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "https://api.usaspending.gov/api/v2/x"})

    c = USAspendingClient()
    c.client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="https://api.usaspending.gov")
    with pytest.raises(NonTransientError):
        await c.get("awards/G1/")
    await c.aclose()


@pytest.mark.asyncio
async def test_token_bucket_burst_then_throttles():
    b = TokenBucket(refill=10, period_s=1.0, burst=2)
    import time as _t
    t0 = _t.monotonic()
    await b.acquire()
    await b.acquire()
    fast = _t.monotonic() - t0
    assert fast < 0.5
    t0 = _t.monotonic()
    await b.acquire()
    assert _t.monotonic() - t0 >= 0.05


# ---- execution -----------------------------------------------------------------

def test_continuation_roundtrip_tamper_expiry_and_scope():
    key = b"test-key-123"
    tok = mint_continuation("run_1", "caller_A", "mh", "qh", {"page": 3}, key)
    ct = ContinuationToken.decode(tok, key)
    assert ct.cursor == {"page": 3} and ct.run_id == "run_1"
    check_scope_binding(ct, "caller_A", "mh", "qh")
    with pytest.raises(ValueError):
        check_scope_binding(ct, "caller_B", "mh", "qh")
    with pytest.raises(ValueError):
        check_scope_binding(ct, "caller_A", "OTHER", "qh")
    # tamper
    body, sig = tok.rsplit(".", 1)
    with pytest.raises(ValueError):
        ContinuationToken.decode(body + "X." + sig, key)
    # expiry
    old = mint_continuation("run_1", "caller_A", "mh", "qh", {}, key, ttl_s=-1)
    with pytest.raises(ValueError):
        ContinuationToken.decode(old, key)
    # version
    tampered = ContinuationToken(run_id="r", caller_id="c", manifest_hash="m",
                                 query_hash="q", created_at=1, expires_at=9999999999,
                                 state_version=999).encode(key)
    with pytest.raises(ValueError):
        ContinuationToken.decode(tampered, key)


def test_budget_exhaustion_reports_reason_never_pass():
    b = BudgetTracker(RunBudgets(max_awards=2, overall_deadline_s=9999))
    assert b.exhausted() is None
    b.awards = 2
    assert b.exhausted() == "max_awards"
    b2 = BudgetTracker(RunBudgets(overall_deadline_s=0))
    assert b2.exhausted() == "overall_deadline"


# ---- startup / auth boundary -----------------------------------------------------

def test_unknown_auth_mode_fails_closed(monkeypatch):
    from usaspending_cscrm_mcp.server_http import create_server
    monkeypatch.setenv("AUTH_MODE", "logingov")
    with pytest.raises(RuntimeError):
        create_server()


def test_health_and_ready_endpoints():
    from starlette.testclient import TestClient

    from usaspending_cscrm_mcp.server_http import app
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "healthy"
    r = client.get("/ready")
    body = r.json()
    assert r.status_code == 200 and body["ready"] is True
    assert body["auth_mode"] == "databricks_passthrough"
