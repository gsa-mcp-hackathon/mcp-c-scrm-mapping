"""Audit certificates: canonical structure + independent verification.

Signing: Ed25519 over the canonical content hash (cryptography library).
The signature envelope carries the full public key as key_id, so any party
holding the certificate alone can verify integrity, gate state, and
authenticity without side channels. Private keys never enter the codebase:
they arrive via CSCRM_SIGNING_KEY (hex seed, minimum 32 bytes) at signing time.

SIGN-BEFORE-ENVELOPE INVARIANT (ordering guarantee):
  1. ``issue_certificate()`` computes ``content_hash`` over the body only
     (``model_dump(exclude={"content_hash", "signature"})``).
  2. ``sign_certificate()`` Ed25519-signs that pre-envelope hash and appends
     the ``SignatureEnvelope`` afterwards; it never recomputes ``content_hash``.
  3. ``verify_certificate()`` recomputes the hash over the same pre-envelope
     projection and Ed25519-verifies the envelope signature against it.
So the hash that is verified is always the pre-envelope hash: mutating any
body field breaks the hash comparison, and mutating the signature breaks
cryptographic verification. Envelope metadata (e.g. the ``reason`` text) is
covered by neither check, by design — it is annotation, not evidence.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, Field

from usaspending_cscrm_mcp.evidence import content_hash


class GateResult(BaseModel):
    gate_id: str
    status: str = "PENDING"
    detail: str = ""


class SignatureEnvelope(BaseModel):
    algorithm: str = "none"
    key_id: str = ""
    signature: str = ""
    status: str = "UNSIGNED"
    reason: str = "signing keys not provisioned (Phase 5)"


class AuditCertificate(BaseModel):
    cert_id: str
    created_at: str = ""
    target_name: str = ""
    window_start: str = ""
    window_end: str = ""
    manifest_hash: str = ""
    matrix_hash: str = ""
    gate_results: list[GateResult] = Field(default_factory=list)
    canary_results: list[GateResult] = Field(default_factory=list)
    totals: dict[str, str] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    content_hash: str = ""
    signature: SignatureEnvelope = Field(default_factory=SignatureEnvelope)


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def issue_certificate(*, target_name: str, window_start: str, window_end: str,
                      manifest_hash: str, matrix_hash: str,
                      gate_results: list[GateResult] | None = None,
                      canary_results: list[GateResult] | None = None,
                      totals: dict[str, str] | None = None,
                      limitations: list[str] | None = None,
                      unresolved: list[str] | None = None) -> AuditCertificate:
    cert = AuditCertificate(
        cert_id="cert_" + content_hash(
            [target_name, window_start, window_end, manifest_hash,
             matrix_hash, utcnow_iso()])[:16],
        created_at=utcnow_iso(), target_name=target_name,
        window_start=window_start, window_end=window_end,
        manifest_hash=manifest_hash, matrix_hash=matrix_hash,
        gate_results=gate_results or [], canary_results=canary_results or [],
        totals=totals or {}, limitations=limitations or [],
        unresolved=unresolved or [])
    cert.content_hash = content_hash(cert.model_dump(
        mode="json", exclude={"content_hash", "signature"}))
    return cert


_ENV_VAR = "CSCRM_SIGNING_KEY"

#: Substrings marking a signing-key value as a placeholder rather than a real
#: random seed. Every entry contains at least one non-hex character, so no
#: valid hex seed can ever match — zero false-positive risk on real keys.
_PLACEHOLDER_MARKERS = (
    "test",
    "changeme",
    "change-me",
    "changethis",
    "password",
    "example",
    "placeholder",
    "default",
    "secret",
    "demo",
    "sample",
)


def _reject_empty_or_placeholder(seed_hex: str) -> str:
    """Strip a raw key value; fail closed on empty or placeholder input."""
    text = (seed_hex or "").strip()
    if not text:
        raise ValueError(
            f"{_ENV_VAR} is empty or whitespace-only: provision a random 32-byte "
            "seed as a Databricks secret (see docs/deployment.md); refusing to sign."
        )
    lowered = text.lower()
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        raise ValueError(
            f"{_ENV_VAR} looks like a placeholder value (contains a well-known "
            "placeholder marker): refusing to sign. Generate a random 32-byte "
            "seed and store it as a Databricks secret; see docs/deployment.md."
        )
    return text


def private_key_from_seed_hex(seed_hex: str) -> Ed25519PrivateKey:
    """Build an Ed25519 key from a hex seed; fail closed naming CSCRM_SIGNING_KEY.

    The seed must decode to a minimum of 32 bytes (generate with
    ``python -c "import secrets; print(secrets.token_hex(32))"``). Longer
    seeds are accepted and deterministically compressed with SHA-256 to the
    32 bytes Ed25519 requires. Empty, placeholder, non-hex, or short values
    raise ValueError — never a weak or default key.
    """
    text = _reject_empty_or_placeholder(seed_hex)
    try:
        seed = bytes.fromhex(text)
    except ValueError as exc:
        raise ValueError(f"{_ENV_VAR} must be hex (got an undecodable value)") from exc
    if len(seed) < 32:
        raise ValueError(
            f"{_ENV_VAR} must decode to at least 32 bytes (got {len(seed)}); "
            "generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if len(seed) > 32:
        seed = hashlib.sha256(seed).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def sign_certificate(cert: AuditCertificate,
                     private_key: Ed25519PrivateKey) -> AuditCertificate:
    """Ed25519-sign the canonical content hash. Returns the same cert, signed.

    Sign-before-envelope: the signature covers the pre-envelope ``content_hash``
    computed by ``issue_certificate()``; the envelope is appended afterwards and
    ``content_hash`` is left untouched (see module docstring invariant).
    """
    public = private_key.public_key()
    pub_hex = public.public_bytes_raw().hex()
    message = cert.content_hash.encode("utf-8")
    cert.signature = SignatureEnvelope(
        algorithm="Ed25519", key_id=pub_hex,
        signature=private_key.sign(message).hex(), status="SIGNED",
        reason="Ed25519 over canonical content_hash; verify with key_id pubkey")
    return cert


def verify_certificate(payload: dict[str, Any]) -> dict[str, Any]:
    """Independently verify a certificate dict. Returns a verdict, never a pass-by-default."""
    try:
        cert = AuditCertificate.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - malformed cert is a finding
        return {"verdict": "INVALID", "reasons": [f"schema: {exc}"]}
    reasons: list[str] = []
    recomputed = content_hash(cert.model_dump(
        mode="json", exclude={"content_hash", "signature"}))
    if recomputed != cert.content_hash:
        reasons.append("content_hash mismatch (payload mutated?)")
    non_pass = [g.gate_id for g in cert.gate_results if g.status != "PASS"]
    if non_pass:
        reasons.append(f"non-PASS gates: {sorted(non_pass)}")
    non_pass_canaries = [c.gate_id for c in cert.canary_results if c.status != "PASS"]
    if non_pass_canaries:
        reasons.append(f"non-PASS canaries: {sorted(non_pass_canaries)}")
    sig = cert.signature
    if sig.status != "SIGNED":
        reasons.append(f"signature: {sig.status} ({sig.reason}) — not independently verifiable")
    elif sig.algorithm != "Ed25519" or not sig.key_id or not sig.signature:
        reasons.append("signature envelope malformed")
    else:
        try:
            pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(sig.key_id))
            pub.verify(bytes.fromhex(sig.signature), cert.content_hash.encode("utf-8"))
        except (ValueError, InvalidSignature) as exc:
            reasons.append(f"signature verification failed: {exc}")
    if reasons:
        return {"verdict": "INVALID", "reasons": reasons}
    return {"verdict": "VALID", "reasons": []}


def signing_key_from_env(env: dict[str, str] | None = None) -> Ed25519PrivateKey | None:
    """Load the signing key from CSCRM_SIGNING_KEY; None when unprovisioned.

    Fail-closed contract: missing or blank → None (callers must take the
    UNSIGNED path, which ``verify_certificate()`` refuses as INVALID).
    Present-but-invalid (placeholder, non-hex, shorter than 32 bytes) →
    ValueError naming CSCRM_SIGNING_KEY, so misconfiguration is loud, never
    a silent fallback to an unsigned or weakly-signed certificate.
    """
    source = env if env is not None else os.environ
    seed_hex = (source.get("CSCRM_SIGNING_KEY") or "").strip()
    if not seed_hex:
        return None
    return private_key_from_seed_hex(seed_hex)


def _git_sha_best_effort() -> str:
    """Best-effort git SHA for provenance; ``"unknown"`` when unavailable.

    Prefers the ``CSCRM_GIT_SHA`` env override (set at build/deploy time),
    then ``git rev-parse HEAD``. Never raises — provenance must not break
    signing when git metadata is absent (e.g. Databricks Apps snapshots).
    """
    override = (os.environ.get("CSCRM_GIT_SHA") or "").strip()
    if override:
        return override
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        sha = (proc.stdout or "").strip()
        if proc.returncode == 0 and sha:
            return sha
    except Exception:  # noqa: BLE001 - SHA is advisory; absence is not an error
        pass
    return "unknown"


def code_version(package: str = "usa-spending-cscrm-mcp-server") -> str:
    """Code version for evidence provenance: ``"<dist-version>+<git-sha>"``.

    Falls back to ``"unknown"`` for either half when it cannot be determined;
    never raises.
    """
    try:
        dist = importlib.metadata.version(package)
    except Exception:  # noqa: BLE001 - version is advisory, not load-bearing
        dist = "unknown"
    return f"{dist}+{_git_sha_best_effort()}"


def _manifest_item_hashes(items: Sequence[Any] | None) -> list[dict[str, str]]:
    """Normalize request/response entries to ``[{hash, retrieved_at}]``.

    Each entry may be a ``{"hash": ..., "retrieved_at": ...}`` dict
    (precomputed hash), a ``{"payload": ..., "retrieved_at": ...}`` dict
    (hash computed canonically via :func:`content_hash`), or a raw payload
    (hash computed, timestamp empty).
    """
    normalized: list[dict[str, str]] = []
    for item in items or []:
        if isinstance(item, dict) and "hash" in item:
            normalized.append({
                "hash": str(item["hash"]),
                "retrieved_at": str(item.get("retrieved_at", "")),
            })
        elif isinstance(item, dict) and "payload" in item:
            normalized.append({
                "hash": content_hash(item["payload"]),
                "retrieved_at": str(item.get("retrieved_at", "")),
            })
        else:
            normalized.append({"hash": content_hash(item), "retrieved_at": ""})
    return normalized


def _adjudication_map(
    adjudications: Mapping[str, Any] | Sequence[Any] | None,
) -> dict[str, Any]:
    """Normalize adjudications to ``{gid: {rule, snippet, ...}}``.

    Accepts a mapping of gid → record (pydantic model, dict, or scalar) or a
    sequence of records carrying a ``gid`` field/attribute. Values are kept
    whole (rule id plus supporting snippet/evidence) so VALID means the full
    adjudication set is covered by the manifest hash.
    """
    if not adjudications:
        return {}
    out: dict[str, Any] = {}
    if isinstance(adjudications, Mapping):
        for gid, adj in adjudications.items():
            if isinstance(adj, BaseModel):
                out[str(gid)] = adj.model_dump(mode="json")
            elif isinstance(adj, dict):
                out[str(gid)] = dict(adj)
            else:
                out[str(gid)] = str(adj)
        return out
    for adj in adjudications:
        if isinstance(adj, BaseModel):
            data = adj.model_dump(mode="json")
            gid = str(data.get("gid", ""))
        elif isinstance(adj, dict):
            data = dict(adj)
            gid = str(adj.get("gid", ""))
        else:
            gid = str(getattr(adj, "gid", ""))
            data = str(adj)
        out[gid or f"item-{len(out)}"] = data
    return out


def build_evidence_manifest(
    *,
    requests: Sequence[Any] | None = None,
    responses: Sequence[Any] | None = None,
    adjudications: Mapping[str, Any] | Sequence[Any] | None = None,
    report_markdown: str | None = None,
    report_hash: str | None = None,
    code_version_str: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build the canonical evidence manifest for end-to-end provenance.

    Covers per-request/per-response hashes with retrieval timestamps, the
    code version (package version plus best-effort git SHA), the full
    adjudication map (rule id plus snippet/evidence per item), and the hash
    of the rendered report markdown — so the human-readable report cannot
    drift from the signed certificate. The manifest is self-hashing:
    ``manifest_hash`` is the canonical :func:`content_hash` of the manifest
    body (every field except ``manifest_hash`` itself).
    """
    resolved_report_hash = report_hash or (
        hashlib.sha256(report_markdown.encode("utf-8")).hexdigest()
        if report_markdown
        else ""
    )
    manifest: dict[str, Any] = {
        "manifest_version": "1",
        "created_at": created_at or utcnow_iso(),
        "code_version": code_version_str or code_version(),
        "requests": _manifest_item_hashes(requests),
        "responses": _manifest_item_hashes(responses),
        "adjudications": _adjudication_map(adjudications),
        "report_hash": resolved_report_hash,
    }
    manifest["manifest_hash"] = content_hash(manifest)
    return manifest
