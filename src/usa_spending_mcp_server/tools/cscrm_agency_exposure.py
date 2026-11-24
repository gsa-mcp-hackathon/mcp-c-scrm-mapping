"""C-SCRM agency exposure discovery tools."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from usa_spending_mcp_server.client import USASpendingClient

logger = logging.getLogger(__name__)

CATEGORY_ENDPOINTS = ("awarding_agency", "awarding_subagency", "funding_agency")
AWARD_FIELDS = [
    "Award ID",
    "Recipient Name",
    "Awarding Agency",
    "Funding Agency",
    "Award Amount",
    "Description",
    "End Date",
    "Recipient UEI",
]


def _today() -> str:
    return date.today().isoformat()


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _is_award_active(end_date_raw: Any, today: date | None = None) -> bool:
    """Return True when an award's Period of Performance end date is on/after today.

    Awards with a missing or unparseable end date are treated as inactive,
    so active_only=True never leaks expired records into C-SCRM evidence.
    """
    pop_end = _parse_date(end_date_raw)
    if pop_end is None:
        return False
    return pop_end >= (today or date.today())


async def _post(client: USASpendingClient, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return await client.post(endpoint, payload)
    except Exception as exc:  # noqa: BLE001 - surface as structured error
        logger.warning("USAspending request failed for %s: %s", endpoint, exc)
        return {"_error": str(exc)}


def register_cscrm_tools(mcp: FastMCP, client: USASpendingClient) -> None:
    @mcp.tool(
        name="discover_agencies_by_target",
        annotations={
            "title": "Discover agencies exposed to a cyber supply chain target",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def discover_agencies_by_target(
        target_query: Annotated[
            str,
            Field(description="Vendor name, product, or service to search (e.g. 'Microsoft', 'SolarWinds', 'cloud hosting')."),
        ],
        search_mode: Annotated[
            Literal["auto", "vendor", "product_or_service"],
            Field(description="'auto' runs both vendor (recipient) and product/service (keyword) vectors; 'vendor' only direct primes; 'product_or_service' only keyword."),
        ] = "auto",
        start_date: Annotated[str, Field(description="Lookback window start (YYYY-MM-DD). Default trailing ~3 FY.")] = "2023-10-01",
        end_date: Annotated[str, Field(description="Lookback window end (YYYY-MM-DD).")] = "2026-12-31",
        active_only: Annotated[bool, Field(description="Keep only award records whose Period of Performance end date is on/after today.")] = False,
        include_idvs: Annotated[bool, Field(description="Also include Indefinite Delivery Vehicles (IDV_A..IDV_E).")] = False,
        target_archetype: Annotated[str, Field(description="Stage-0 archetype: 'pure_play_vendor', 'conglomerate_product', 'service_or_integrator', or 'unknown'. Gating applies to conglomerate/product targets.")] = "unknown",
        allow_conglomerate_recipient_search: Annotated[bool, Field(description="Override: run recipient_search_text on the raw target_query even for conglomerate/product targets (contamination risk acknowledged).")] = False,
        division_names: Annotated[list[str] | None, Field(description="Pure-play division DBA names (e.g. ['Software House']). For conglomerate targets, the vendor vector runs on THESE instead of the parent name.")] = None,
    ) -> dict[str, Any]:
        """Discover which federal agencies are exposed to a cyber supply chain target.

        Queries the live USAspending.gov v2 API (no mocked data). In 'auto' mode runs
        concurrent dual-vector queries: recipient_search_text (direct vendor primes) and
        keywords (products/services bought via resellers) across awarding_agency,
        awarding_subagency, funding_agency spending_by_category endpoints plus
        spending_by_award (sorted by Award Amount desc).

        Fix 2 (conglomerate gating): when target_archetype is a conglomerate-owned
        product/division, the recipient_search_text vector runs ONLY on the
        pure-play division DBA names (division_names, e.g. 'Software House') —
        never on the parent name ('Johnson Controls', which returns HVAC/chiller
        cross-division noise). With no division names it is SKIPPED unless
        explicitly allowed. Use the Stage 0 manifest's UEI-scoped search instead.
        """
        award_types = ["A", "B", "C", "D"]
        if include_idvs:
            award_types = award_types + ["IDV_A", "IDV_B", "IDV_C", "IDV_D", "IDV_E"]

        vectors: list[tuple[str, str, str]] = []
        recipient_vector_skipped: str | None = None
        recipient_vector_queries: list[str] = [target_query]
        if search_mode in ("auto", "vendor"):
            if (target_archetype in ("conglomerate_product", "service_or_integrator")
                    and not allow_conglomerate_recipient_search):
                divs = [d for d in (division_names or []) if d]
                if divs:
                    recipient_vector_queries = divs
                    for d in divs:
                        vectors.append((f"vendor:{d}", "recipient_search_text", d))
                else:
                    recipient_vector_skipped = (
                        f"recipient_search_text vector skipped: target_archetype="
                        f"'{target_archetype}' risks cross-division contamination; "
                        f"pass division_names (Stage 0 oem_division_names) to run it on the "
                        f"pure-play division, or set allow_conglomerate_recipient_search=true."
                    )
            else:
                vectors.append(("vendor", "recipient_search_text", target_query))
        if search_mode in ("auto", "product_or_service"):
            vectors.append(("product_or_service", "keywords", target_query))

        if not vectors:
            vectors = [("vendor", "recipient_search_text", target_query)]

        today = date.today()

        # Build concurrent tasks
        tasks: list[asyncio.Task] = []

        async def run_category(vector_label: str, filter_key: str, query: str, cat: str):
            filters = {
                "time_period": [{"start_date": start_date, "end_date": end_date}],
                "award_type_codes": award_types,
                filter_key: [query],
            }
            payload = {"filters": filters, "limit": 100, "page": 1}
            endpoint = f"search/spending_by_category/{cat}/"
            resp = await _post(client, endpoint, payload)
            return ("category", vector_label, filter_key, cat, resp)

        # Vehicle groups must be queried separately: spending_by_award
        # returns HTTP 422 when contract codes (A-D) are mixed with IDV
        # codes in one call. Category endpoints accept the mixed set.
        award_partial_errors: list[str] = []

        async def run_award(vector_label: str, filter_key: str, query: str):
            groups = [
                ("contracts", [c for c in award_types if not c.startswith("IDV")]),
                ("idvs", [c for c in award_types if c.startswith("IDV")]),
            ]
            merged: list[dict[str, Any]] = []
            for group_name, codes in groups:
                if not codes:
                    continue
                filters = {
                    "time_period": [{"start_date": start_date, "end_date": end_date}],
                    "award_type_codes": codes,
                    filter_key: [query],
                }
                payload = {
                    "filters": filters,
                    "fields": AWARD_FIELDS,
                    "sort": "Award Amount",
                    "order": "desc",
                    "limit": 25,
                    "page": 1,
                }
                resp = await _post(client, "search/spending_by_award/", payload)
                if isinstance(resp, dict) and "_error" in resp:
                    award_partial_errors.append(
                        f"award/{group_name} ({vector_label}): {resp['_error']}"
                    )
                    continue
                if isinstance(resp, dict):
                    merged.extend(resp.get("results", []))
            if not merged and award_partial_errors:
                return ("award", vector_label, filter_key, None, {"_error": "all award vehicle groups failed"})
            merged.sort(key=lambda r: (r.get("Award Amount") or 0), reverse=True)
            return (
                "award",
                vector_label,
                filter_key,
                None,
                {"results": merged[:50], "page_metadata": {"hasNext": False}},
            )

        for vector_label, filter_key, query in vectors:
            for cat in CATEGORY_ENDPOINTS:
                tasks.append(asyncio.ensure_future(run_category(vector_label, filter_key, query, cat)))
            tasks.append(asyncio.ensure_future(run_award(vector_label, filter_key, query)))

        gathered = await asyncio.gather(*tasks, return_exceptions=True)

        # Aggregation containers
        awarding_names: set[str] = set()
        funding_names: set[str] = set()
        sub_agencies: list[dict[str, Any]] = []
        obligated_rows: list[dict[str, Any]] = []
        award_records: list[dict[str, Any]] = []
        warnings: list[str] = []
        errors: list[str] = []
        errors.extend(e for e in award_partial_errors if e not in errors)

        for res in gathered:
            if isinstance(res, Exception):
                errors.append(str(res))
                continue
            kind, vector_label, filter_key, cat, resp = res
            if isinstance(resp, dict) and "_error" in resp:
                errors.append(f"{kind}/{cat or 'award'} ({vector_label}): {resp['_error']}")
                continue
            if kind == "category":
                results = resp.get("results", []) if isinstance(resp, dict) else []
                if cat == "awarding_agency":
                    for row in results:
                        name = row.get("name")
                        if name:
                            awarding_names.add(name)
                            obligated_rows.append({
                                "agency_name": name,
                                "agency_type": "awarding_agency",
                                "amount": row.get("amount"),
                                "vector": vector_label,
                            })
                elif cat == "awarding_subagency":
                    for row in results:
                        sub_name = row.get("name")
                        parent = row.get("agency_name")
                        if sub_name:
                            sub_agencies.append({
                                "sub_agency_name": sub_name,
                                "parent_agency_name": parent,
                                "amount": row.get("amount"),
                                "vector": vector_label,
                            })
                        if parent:
                            awarding_names.add(parent)
                elif cat == "funding_agency":
                    for row in results:
                        name = row.get("name")
                        if name:
                            funding_names.add(name)
                            obligated_rows.append({
                                "agency_name": name,
                                "agency_type": "funding_agency",
                                "amount": row.get("amount"),
                                "vector": vector_label,
                            })
            else:
                results = resp.get("results", []) if isinstance(resp, dict) else []
                for row in results:
                    award_id = row.get("Award ID")
                    recipient_name = row.get("Recipient Name")
                    recipient_uei = row.get("Recipient UEI")
                    awarding_agency = row.get("Awarding Agency")
                    funding_agency = row.get("Funding Agency")
                    award_amount = row.get("Award Amount")
                    description = row.get("Description")
                    end_date_raw = row.get("End Date")

                    if active_only and not _is_award_active(end_date_raw, today):
                        continue

                    if awarding_agency:
                        awarding_names.add(awarding_agency)
                    if funding_agency:
                        funding_names.add(funding_agency)

                    award_records.append({
                        "award_id": award_id,
                        "generated_internal_id": row.get("generated_internal_id"),
                        "recipient_name": recipient_name,
                        "recipient_uei": recipient_uei,
                        "awarding_agency": awarding_agency,
                        "funding_agency": funding_agency,
                        "award_amount": award_amount,
                        "description": description,
                        "period_of_performance_end_date": end_date_raw,
                        "vector": vector_label,
                    })

        # Reseller UEIs: from product_or_service vector award records (and vendor too) — collect unique
        reseller_ueis: list[dict[str, Any]] = []
        seen_uei: set[str] = set()
        for rec in award_records:
            uei = rec.get("recipient_uei")
            if uei and uei not in seen_uei:
                seen_uei.add(uei)
                reseller_ueis.append({
                    "uei": uei,
                    "recipient_name": rec.get("recipient_name"),
                    "awarding_agency": rec.get("awarding_agency"),
                    "award_id": rec.get("award_id"),
                    "vector": rec.get("vector"),
                })

        agencies_using = sorted(awarding_names | funding_names)

        payload: dict[str, Any] = {
            "target_query": target_query,
            "search_mode": search_mode,
            "window": {"start_date": start_date, "end_date": end_date},
            "award_type_codes": award_types,
            "active_only": active_only,
            "include_idvs": include_idvs,
            "as_of_date": today.isoformat(),
            "agencies_using_target": agencies_using,
            "awarding_agency_names": sorted(awarding_names),
            "funding_agency_names": sorted(funding_names),
            "evidence": {
                "obligated_amounts": obligated_rows,
                "sub_agencies": sub_agencies,
                "fulfilling_reseller_ueis": reseller_ueis,
                "representative_awards": award_records,
            },
            "counts": {
                "agencies_using_target": len(agencies_using),
                "awarding_agencies": len(awarding_names),
                "funding_agencies": len(funding_names),
                "sub_agencies": len(sub_agencies),
                "obligated_rows": len(obligated_rows),
                "representative_awards": len(award_records),
                "reseller_ueis": len(reseller_ueis),
            },
        }
        if errors:
            payload["errors"] = errors
        if warnings:
            payload["warnings"] = warnings
        payload["recipient_vector_status"] = (
            recipient_vector_skipped or
            f"recipient_search_text vector executed on: {recipient_vector_queries}"
        )
        if recipient_vector_skipped:
            payload.setdefault("warnings", []).append(recipient_vector_skipped)
        return payload
