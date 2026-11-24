"""C-SCRM tool-invocation benchmark: exercises both MCP tools live and scores them.

Runs a fixed case list against the live USAspending.gov v2 API (no mocks),
times each invocation, and checks deterministic expectations (known agencies,
UEIs, hierarchy codes). Prints a per-case table plus a JSON summary, and exits
nonzero if any case fails.

Usage::

    python evals/cscrm_benchmark.py [--json-out evals/last_run.json]

Optional LLM-judge mode (mirrors the usa-spending DEMO evals setup)::

    ANTHROPIC_API_KEY=... python evals/cscrm_benchmark.py --llm-judge

LLM-judge mode asks the model whether each tool payload plausibly answers the
case prompt; without it, scoring is purely rule-based and needs no API key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastmcp import FastMCP  # noqa: E402
from fastmcp.client import Client  # noqa: E402

from usa_spending_mcp_server.client import USASpendingClient  # noqa: E402
from usa_spending_mcp_server.tools import register_tools  # noqa: E402

CASES = [
    {
        "name": "vendor_microsoft_fy24",
        "prompt": "Which agencies buy from Microsoft (direct primes, FY2024)?",
        "tool": "discover_agencies_by_target",
        "arguments": {
            "target_query": "Microsoft",
            "search_mode": "vendor",
            "start_date": "2023-10-01",
            "end_date": "2024-09-30",
        },
        "expects": {
            "agencies_contains": ["Department of Defense"],
            "min_awards": 1,
        },
    },
    {
        "name": "product_cloud_reseller",
        "prompt": "Which agencies buy cloud products, including via resellers?",
        "tool": "discover_agencies_by_target",
        "arguments": {
            "target_query": "cloud",
            "search_mode": "product_or_service",
            "start_date": "2024-10-01",
            "end_date": "2025-09-30",
        },
        "expects": {
            "agencies_contains": ["Department of Defense"],
            "min_awards": 1,
        },
    },
    {
        "name": "award_detail_microsoft_do",
        "prompt": "Deep-inspect high-value Microsoft delivery order HC102822F0982.",
        "tool": "get_award_cscrm_details",
        "arguments": {
            "generated_internal_id": "CONT_AWD_HC102822F0982_9700_HC102817D0001_9700",
        },
        "expects": {
            "parent_recipient_uei": "NX52EXRHRDK7",
            "naics_base": "541512",
        },
    },
]


def score_case(case: dict, payload: dict) -> tuple[bool, list[str]]:
    """Rule-based scoring; returns (passed, notes)."""
    notes: list[str] = []
    ok = True
    expects = case["expects"]

    if "agencies_contains" in expects:
        agencies = payload.get("agencies_using_target", [])
        for want in expects["agencies_contains"]:
            if want in agencies:
                notes.append(f"agency present: {want}")
            else:
                ok = False
                notes.append(f"MISSING agency: {want}")

    if "min_awards" in expects:
        n = payload.get("counts", {}).get("representative_awards", 0)
        if n >= expects["min_awards"]:
            notes.append(f"awards={n}")
        else:
            ok = False
            notes.append(f"too few awards: {n}")

    if "parent_recipient_uei" in expects:
        got = payload.get("parent_recipient_uei")
        if got == expects["parent_recipient_uei"]:
            notes.append(f"parent UEI match: {got}")
        else:
            ok = False
            notes.append(f"parent UEI mismatch: {got!r}")

    if "naics_base" in expects:
        levels = payload.get("naics_levels", [])
        base = levels[-1]["code"] if levels else None
        if base == expects["naics_base"]:
            notes.append(f"NAICS base match: {base}")
        else:
            ok = False
            notes.append(f"NAICS base mismatch: {base!r}")

    if payload.get("errors"):
        ok = False
        notes.append(f"tool errors: {payload['errors']}")

    return ok, notes


async def maybe_llm_judge(prompt: str, payload: dict) -> str | None:
    """Optional LLM relevance verdict; None when no API key is configured."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    import urllib.request

    body = json.dumps(
        {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 100,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Question: " + prompt + "\n\nTool payload (truncated): "
                        + json.dumps(payload)[:4000]
                        + "\n\nDoes the payload plausibly answer the question? "
                        "Reply VERDICT: PASS or VERDICT: FAIL plus one sentence."
                    ),
                }
            ],
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    return "".join(b.get("text", "") for b in data.get("content", []))


async def run_case(client_mcp: Client, case: dict, llm_judge: bool) -> dict:
    started = time.perf_counter()
    try:
        res = await client_mcp.call_tool(case["tool"], case["arguments"])
        payload = res.structured_content or {}
        transport_error = None
    except Exception as exc:  # noqa: BLE001 - recorded in the report
        payload, transport_error = {}, str(exc)
    elapsed = time.perf_counter() - started

    if transport_error:
        return {"name": case["name"], "passed": False, "seconds": round(elapsed, 2),
                "notes": [f"transport error: {transport_error}"], "llm": None}

    passed, notes = score_case(case, payload)
    llm = await maybe_llm_judge(case["prompt"], payload) if llm_judge else None
    return {"name": case["name"], "passed": passed, "seconds": round(elapsed, 2),
            "notes": notes, "llm": llm}


async def main() -> int:
    parser = argparse.ArgumentParser(description="C-SCRM MCP tool benchmark")
    parser.add_argument("--json-out", default=None, help="Write JSON report to this path")
    parser.add_argument("--llm-judge", action="store_true", help="Also score with an LLM (needs ANTHROPIC_API_KEY)")
    args = parser.parse_args()

    results: list[dict] = []
    async with USASpendingClient() as http_client:
        mcp = FastMCP("cscrm-benchmark")
        register_tools(mcp, http_client)
        async with Client(transport=mcp) as c:
            for case in CASES:
                results.append(await run_case(c, case, args.llm_judge))

    passed = sum(1 for r in results if r["passed"])
    total_s = sum(r["seconds"] for r in results)
    print(f"\n{'case':<28}{'pass':<8}{'secs':<8}notes")
    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"{r['name']:<28}{mark:<8}{r['seconds']:<8}{'; '.join(r['notes'])}")
        if r["llm"]:
            print(f"  llm-judge: {r['llm']}")
    print(f"\n{passed}/{len(results)} cases passed in {total_s:.1f}s")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"results": results, "passed": passed, "total": len(results)}, fh, indent=2)
        print(f"report: {args.json_out}")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
