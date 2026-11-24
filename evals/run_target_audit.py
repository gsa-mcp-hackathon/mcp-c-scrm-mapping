"""Target audit runner: triage -> manifest -> audit_run -> report bundle.

Runs the real v2 tool path in-process (FastMCP client, live USAspending API)
and writes a QA-able bundle: report markdown + manifest/cert/gates JSON.

Usage:
    PYTHONPATH=src python evals/run_target_audit.py --target "Jamf Protect" \\
        --terms "Jamf Protect,Jamf Pro,Jamf Connect,Jamf" \\
        --outdir "C:/Users/User/Desktop/fed_procurement_deliverables"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastmcp import Client  # noqa: E402

from usaspending_cscrm_mcp.certification import verify_certificate  # noqa: E402
from usaspending_cscrm_mcp.server import create_server  # noqa: E402
from usaspending_cscrm_mcp.tools import ServerDeps, configure  # noqa: E402


def slug(name: str) -> str:
    return "".join(c.lower() if c.isalnum() else "_" for c in name).strip("_")


def money(v: str) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


async def run(target: str, terms: list[str], archetype: str, start: str,
              end: str, ueis: list[str] | None = None,
              domain_nouns: list[str] | None = None) -> dict:
    configure(ServerDeps())
    async with Client(create_server()) as client:
        tri = await client.call_tool("target_triage", {
            "target_query": target, "candidate_terms": terms,
            "target_archetype": archetype,
            "verified_oem_ueis": ueis or [],
            "product_terms": [target],
            "domain_nouns": domain_nouns or [],
            "from_date": start, "to_date": end})
        tdata = tri.data
        if tdata["status"] != "READY" or not tdata["manifest"]:
            return {"triage": tdata, "audit": None}
        audit = await client.call_tool("audit_run", {
            "target_manifest": tdata["manifest"],
            "sweep_terms": tdata["manifest"]["standalone_terms"],
            "include_subawards": True, "run_canaries": False})
        return {"triage": tdata, "audit": audit.data}


NOT_REPORTED = "Not reported by source"


def _cell(value: object) -> str:
    """Render one table cell; missing values are never blank, never invented."""
    text = "" if value is None else str(value).strip().replace("|", "/").replace("\n", " ")
    return text if text else NOT_REPORTED


def parse_at10_detail(detail: str | None) -> dict:
    """Parse the AT-10 embargo detail string into a first-class signal.

    AT-10 packs its flag into prose ("embargo_cutoff=<date>
    banner_needed=<bool> recompete_watch=<list>"); this is the single
    sanctioned parser, and the renderer keys its banner callout off it.
    Missing/unparseable input means no banner.
    """
    text = detail or ""
    cutoff = re.search(r"embargo_cutoff\s*=\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", text)
    flag = re.search(r"banner_needed\s*=\s*([A-Za-z0-9]+)", text)
    watch = re.search(r"recompete_watch\s*=\s*(.+?)\s*$", text.strip())
    raw = flag.group(1).lower() if flag else ""
    return {
        "embargo_cutoff": cutoff.group(1) if cutoff else None,
        "banner_needed": raw in ("true", "1", "yes"),
        "recompete_watch": watch.group(1).strip() if watch else None,
    }


def _embargo_banner(audit: dict, window_end: str) -> str | None:
    """Top-of-report embargo callout, or None when AT-10 needs no banner."""
    for gate in audit.get("gates", []) or []:
        if gate.get("gate_id") != "AT-10":
            continue
        parsed = parse_at10_detail(gate.get("detail", ""))
        if not parsed["banner_needed"]:
            return None
        cutoff = parsed["embargo_cutoff"] or "unknown cutoff"
        watch = parsed["recompete_watch"] or "none"
        return (f"> EMBARGO / STALENESS NOTICE — window end {window_end} falls after "
                f"the embargo cutoff {cutoff}: recent awards may be missing or revised "
                f"as reporting lands. Recompete watch: {watch}. "
                f"(AT-10 is advisory; figures below exclude unreported activity.)")
    return None


def render_report(target: str, start: str, end: str, triage: dict,
                  audit: dict | None) -> str:
    lines = [f"# C-SCRM Intelligence Report: {target}",
             f"_Window {start} to {end} · generated {date.today().isoformat()} · "
             "v2 pipeline (Stages 0-6 + AT-00..AT-10)_", ""]
    m = triage.get("manifest") or {}
    if audit is not None:
        _banner = _embargo_banner(audit, window_end=end)
        if _banner:
            lines += ["", _banner]
    lines += ["## §0 Scope lock (Stage 0)",
              f"Status: **{triage.get('status')}** · "
              f"manifest hash `{triage.get('manifest_hash', '')[:16]}…`",
              f"Archetype: {m.get('target_archetype')} · "
              f"conglomerate={m.get('is_conglomerate')}"
              + (f" · effective={m.get('effective_archetype')}"
                 if m.get("effective_archetype") and
                 m.get("effective_archetype") != m.get("target_archetype") else "")
              + (f" · siblings={', '.join(m.get('sibling_terms') or [])}"
                 if m.get("sibling_terms") else ""),
              "", "| term | disposition | precision | n |",
              "|---|---|---|---|"]
    for s in m.get("term_scores", []):
        lines.append(f"| {s['term']} | {s['disposition']} | {s['precision']} | "
                     f"{s['sample_size']} |")
    if m.get("rejected_terms"):
        lines.append(f"\nRejected: {', '.join(m['rejected_terms'])}")
    if m.get("unresolved_limitations"):
        lines.append(f"Unresolved: {'; '.join(m['unresolved_limitations'])}")
    if audit is None:
        lines += ["", "> Audit not run: triage did not reach READY. "
                 "Supply discriminating evidence and re-run."]
        return "\n".join(lines)
    lines += ["", f"## Audit verdict: **{audit.get('overall')}** "
             f"(status {audit.get('status')})", ""]
    totals = (audit.get("matrix") or {}).get("totals", {})
    lines.append("### Tier totals (in-window net): "
                 + (", ".join(f"{k}: {money(v)}" for k, v in totals.items())
                    or "none"))
    lines += ["", "## §1 Agency exposure matrix", "",
              "| agency | cohort | awards | in-window net | lifetime | proofs |",
              "|---|---|---|---|---|---|"]
    for row in (audit.get("matrix") or {}).get("matrix", []):
        lines.append(f"| {row['agency']} | {row['display']} | {row['award_count']} | "
                     f"{money(row['window_net'])} | {money(row['lifetime_total'])} | "
                     f"{len(row['proof_gids'])}{'...' if row.get('proof_truncated') else ''} |")
    for row in (audit.get("matrix") or {}).get("servicing_ledger", []):
        lines.append(f"| {row['funding_agency']} (via {row['servicing_agency']}) | "
                     f"servicing | {row['award_count']} | — | — | "
                     f"{len(row['proof_gids'])} |")
    sub_cohorts = (audit.get("matrix") or {}).get("subaward_cohorts", [])
    if sub_cohorts:
        lines += ["",
                  "_Subaward exposure (segregated — subaward dollars are never "
                  "added to prime totals):_", ""]
        for row in sub_cohorts:
            proofs = row.get("proof_gids", [])
            lines.append(f"| {row.get('agency', '')} | {row.get('display', '')} | "
                         f"{row.get('award_count', 0)} | {money(row['window_net'])} | "
                         f"{money(row['lifetime_total'])} | "
                         f"{len(proofs)}{'...' if row.get('proof_truncated') else ''} |")
        for row in sub_cohorts:
            if row.get("source_note"):
                lines.append(f"- {row.get('agency', '')}: {row['source_note']}")
    ledger = audit.get("ledger", [])
    tier1 = [e for e in ledger if e["display"] in ("Tier-1A", "Tier-1B")]
    lines += ["", f"## §2 Tier-1 award ledger ({len(tier1)} awards)", "",
              "| piid | recipient | funder | in-window | lifetime | tier | rule | evidence |",
              "|---|---|---|---|---|---|---|---|"]
    for e in tier1[:50]:
        lines.append(f"| {e['piid']} | {e['recipient_name']} | {e['funding_agency']} | "
                     f"{money(e['window_net'])} | {money(e['lifetime_obligated'])} | "
                     f"{e['tier']} | {e['rule']} | {e['snippet'][:100]} |")
    if len(tier1) > 50:
        lines.append(f"\n_Top 50 shown; full ledger ({len(tier1)}) in bundle JSON._")
    for title, pred in (("§3 Historical ledger", lambda e: e["display"] == "HISTORICAL"),
                        ("§4 Dropped sample", lambda e: e["tier"].startswith("DROPPED"))):
        group = [e for e in ledger if pred(e)]
        lines += ["", f"## {title} ({len(group)} awards)"]
        if title.startswith("§3"):
            lines += ["", "| piid | recipient | reason | in-window | lifetime |",
                      "|---|---|---|---|---|"]
            for e in group[:30]:
                lines.append(f"| {e['piid']} | {e['recipient_name']} | "
                             f"{e['ghost_reason']} | {money(e['window_net'])} | "
                             f"{money(e['lifetime_obligated'])} |")
    subs = audit.get("subawards") or {}
    kept = subs.get("kept_hits", [])
    lines += ["", f"## §5 Subawards (segregated, {len(kept)} kept hits)"]
    if subs.get("sweep_warning"):
        lines.append(f"Sweep warning: {subs['sweep_warning']}")
    lines += ["", "| sub-award | subawardee | amount | prime | tier | agency | description |",
              "|---|---|---|---|---|---|---|"]
    for h in kept[:25]:
        amt = h.get("amount")
        lines.append(f"| {_cell(h.get('sub_award_id'))} | {_cell(h.get('subawardee_name'))} | "
                     f"{money(str(amt)) if amt is not None else NOT_REPORTED} | "
                     f"{_cell(h.get('prime_gid') or h.get('prime_award_id'))} | "
                     f"{_cell(h.get('tier'))} | "
                     f"{_cell(h.get('awarding_agency') or h.get('funding_agency'))} | "
                     f"{_cell(h.get('description'))} |")
    lines += ["", "## §6 Gates AT-00..AT-10", "",
              "| gate | status | detail |", "|---|---|---|"]
    for g in audit.get("gates", []):
        lines.append(f"| {g['gate_id']} | {g['status']} | {g['detail'][:200]} |")
    lines += ["", "## §7 Certificate",
              f"cert `{audit['certificate']['cert_id']}` · "
              f"content hash `{audit['certificate']['content_hash'][:16]}…` · "
              f"signature: **{audit['certificate']['signature']['status']}** "
              f"({audit['certificate']['signature']['algorithm']})"]
    verdict = verify_certificate(audit["certificate"])
    lines.append(f"Independent verification: **{verdict['verdict']}**"
                 + (f" — {'; '.join(verdict['reasons'])}" if verdict["reasons"] else ""))
    comp = (audit.get("matrix") or {}).get("completeness", {})
    lines += ["", "## §8 Completeness",
              f"populations {comp.get('populations_complete', 0)}/"
              f"{comp.get('populations_queried', 0)} complete · "
              f"hydrated {comp.get('awards_hydrated', 0)} · "
              f"failed {comp.get('awards_failed', 0)}"]
    for v in comp.get("parity_violations", [])[:10]:
        lines.append(f"- {v}")
    for u in comp.get("unresolved_limitations", []):
        lines.append(f"- unresolved: {u}")
    lines += ["",
              "_Methods: procurement records establish documented award associations — "
              "not necessarily installed products, current operational dependencies, "
              "or product-exclusive spending. Monetary basis per row: see ledger "
              "`basis` (vendor-associated vs product-associated)._"]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--terms", required=True, help="comma-separated candidate terms")
    ap.add_argument("--archetype", default="ARCHETYPE_1_PURE_PLAY_VENDOR")
    ap.add_argument("--from-date", default="2023-10-01")
    ap.add_argument("--to-date", default="2026-09-30")
    ap.add_argument("--ueis", default="",
                    help="comma-separated verified OEM UEIs (signal A)")
    ap.add_argument("--domain-nouns", default="",
                    help="comma-separated product-domain nouns for signal B "
                         "(e.g. valve,compressor,chiller for HVAC vendors)")
    ap.add_argument("--outdir", required=True)
    args = ap.parse_args()
    terms = [t.strip() for t in args.terms.split(",") if t.strip()]
    ueis = [u.strip() for u in args.ueis.split(",") if u.strip()]
    nouns = [n.strip() for n in args.domain_nouns.split(",") if n.strip()]
    result = asyncio.run(run(args.target, terms, args.archetype,
                             args.from_date, args.to_date,
                             ueis=ueis, domain_nouns=nouns))
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    base = f"{slug(args.target)}_{stamp}"
    (outdir / f"{base}_triage.json").write_text(
        json.dumps(result["triage"], indent=2, default=str))
    if result["audit"] is not None:
        (outdir / f"{base}_certificate.json").write_text(
            json.dumps(result["audit"]["certificate"], indent=2, default=str))
        (outdir / f"{base}_bundle.json").write_text(
            json.dumps(result["audit"], indent=2, default=str))
    report = render_report(args.target, args.from_date, args.to_date,
                           result["triage"], result["audit"])
    (outdir / f"{base}_report.md").write_text(report)
    audit = result["audit"] or {}
    print(f"triage={result['triage']['status']} "
          f"overall={audit.get('overall')} "
          f"gates={[g['gate_id'] + ':' + g['status'] for g in audit.get('gates', [])]}")
    print(f"wrote {base}_report.md (+ json)")


if __name__ == "__main__":
    main()
