"""Phase 5 signing hardening: key validation, envelope ordering, manifest, VALID path.

Companion to the pinned INVALID tests in test_v2_phase5_gates.py and
test_v2_phase3_stage56.py (fresh/UNSIGNED certs refuse as INVALID; mutated or
forged payloads refuse as INVALID). This module adds the VALID
self-verification path with a temporary key plus fail-closed key validation.
"""

import hashlib
import secrets

import pytest

from usaspending_cscrm_mcp.certification import (
    GateResult,
    build_evidence_manifest,
    code_version,
    issue_certificate,
    private_key_from_seed_hex,
    sign_certificate,
    signing_key_from_env,
    verify_certificate,
)
from usaspending_cscrm_mcp.evidence import content_hash


def _passing_cert():
    return issue_certificate(
        target_name="Z",
        window_start="2023-10-01",
        window_end="2026-09-30",
        manifest_hash="mh",
        matrix_hash="mx",
        gate_results=[GateResult(gate_id="AT-00", status="PASS")],
        totals={"Tier-1A": "100"},
    )


def _temp_key():
    key = signing_key_from_env({"CSCRM_SIGNING_KEY": secrets.token_hex(32)})
    assert key is not None
    return key


def test_valid_self_verification_with_temp_key():
    key = _temp_key()
    assert key is not None
    cert = _passing_cert()
    # UNSIGNED pins INVALID before signing (fail-closed no-key path).
    assert verify_certificate(cert.model_dump(mode="json"))["verdict"] == "INVALID"
    sign_certificate(cert, key)
    assert cert.signature.status == "SIGNED"
    assert verify_certificate(cert.model_dump(mode="json"))["verdict"] == "VALID"


def test_missing_or_blank_key_is_unprovisioned():
    assert signing_key_from_env({}) is None
    assert signing_key_from_env({"CSCRM_SIGNING_KEY": "   "}) is None


@pytest.mark.parametrize(
    "bad",
    [
        "test",
        "TEST-KEY",
        "changeme",
        "ChangeMe123",
        "password123",
        "example-secret",
        "placeholder",
        "demo-key",
    ],
)
def test_placeholder_keys_rejected_fail_closed(bad):
    with pytest.raises(ValueError, match="CSCRM_SIGNING_KEY"):
        private_key_from_seed_hex(bad)
    with pytest.raises(ValueError, match="CSCRM_SIGNING_KEY"):
        signing_key_from_env({"CSCRM_SIGNING_KEY": bad})


def test_short_or_nonhex_key_rejected_fail_closed():
    with pytest.raises(ValueError, match="CSCRM_SIGNING_KEY"):
        private_key_from_seed_hex("zz")
    with pytest.raises(ValueError, match="CSCRM_SIGNING_KEY"):
        private_key_from_seed_hex("ab" * 10)  # 10 bytes < 32-byte minimum
    with pytest.raises(ValueError, match="CSCRM_SIGNING_KEY"):
        signing_key_from_env({"CSCRM_SIGNING_KEY": "ab" * 31})  # 31 bytes


def test_minimum_32_bytes_and_longer_seeds_accepted():
    for seed_hex in ("ab" * 32, "ab" * 48):  # exactly 32; longer derives via SHA-256
        key = signing_key_from_env({"CSCRM_SIGNING_KEY": seed_hex})
        assert key is not None
        cert = _passing_cert()
        sign_certificate(cert, key)
        assert verify_certificate(cert.model_dump(mode="json"))["verdict"] == "VALID"


def test_sign_before_envelope_invariant():
    key = _temp_key()
    cert = _passing_cert()
    pre_hash = cert.content_hash
    sign_certificate(cert, key)
    # Signing appends the envelope without rehashing: hash verified is the
    # pre-envelope hash.
    assert cert.content_hash == pre_hash
    assert verify_certificate(cert.model_dump(mode="json"))["verdict"] == "VALID"
    # Mutating the body breaks the hash comparison.
    tampered = cert.model_dump(mode="json")
    tampered["totals"] = {"Tier-1A": "101"}
    assert verify_certificate(tampered)["verdict"] == "INVALID"
    # Mutating the signature breaks cryptographic verification.
    forged_key = cert.model_dump(mode="json")
    forged_key["signature"]["key_id"] = "00" * 32
    assert verify_certificate(forged_key)["verdict"] == "INVALID"


def test_evidence_manifest_covers_required_fields():
    manifest = build_evidence_manifest(
        requests=[{"payload": {"target": "Z"}, "retrieved_at": "2026-11-01T00:00:00Z"}],
        responses=[{"hash": "abc123", "retrieved_at": "2026-11-01T00:00:01Z"}],
        adjudications={"G1": {"rule": "KEEP_X", "snippet": "zorpcore"}},
        report_markdown="# report",
    )
    assert manifest["requests"][0]["retrieved_at"] == "2026-11-01T00:00:00Z"
    assert manifest["requests"][0]["hash"] == content_hash({"target": "Z"})
    assert manifest["responses"][0] == {"hash": "abc123", "retrieved_at": "2026-11-01T00:00:01Z"}
    assert manifest["adjudications"]["G1"]["snippet"] == "zorpcore"
    assert manifest["report_hash"] == hashlib.sha256(b"# report").hexdigest()
    assert manifest["code_version"] and "+" in manifest["code_version"]
    body = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    assert manifest["manifest_hash"] == content_hash(body)
    # Tampering with any covered field breaks the manifest hash.
    tampered = dict(manifest)
    tampered["report_hash"] = "00" * 32
    tampered_body = {k: v for k, v in tampered.items() if k != "manifest_hash"}
    assert tampered["manifest_hash"] != content_hash(tampered_body)


def test_evidence_manifest_defaults_and_code_version_never_raise():
    manifest = build_evidence_manifest()
    assert manifest["requests"] == [] and manifest["responses"] == []
    assert manifest["adjudications"] == {} and manifest["report_hash"] == ""
    assert manifest["manifest_hash"] == content_hash(
        {k: v for k, v in manifest.items() if k != "manifest_hash"}
    )
    assert isinstance(code_version("definitely-not-a-package-xyz"), str)
