"""Phase 6 live contract probes (read-only; skip on transient network weather)."""

import pytest

from usaspending_cscrm_mcp.client import (
    NonTransientError,
    TransportError,
    USAspendingClient,
)

SIEMENS = "CONT_AWD_36C25521P0052_3600_-NONE-_-NONE-"
CDC_JCI = "CONT_AWD_75D30124F00014_7523_GS07F190CA_4732"
JCI_UEI = "CE8MGXAS9KJ6"


def live_client() -> USAspendingClient:
    return USAspendingClient(timeout=60.0, max_concurrency=4)


async def guarded(coro):
    try:
        return await coro
    except TransportError as exc:
        if exc.is_transient:
            pytest.skip(f"live USAspending API unavailable: {exc}")
        raise


@pytest.mark.asyncio
async def test_search_shape_and_codes_required():
    c = live_client()
    try:
        data = await guarded(c.post("search/spending_by_award/", {
            "filters": {"keywords": ["C-CURE 9000"],
                        "award_type_codes": ["A", "B", "C", "D"]},
            "fields": ["Award ID", "Award Amount", "generated_internal_id"],
            "sort": "Award Amount", "order": "desc", "limit": 5, "page": 1}))
        assert isinstance(data.get("results"), list) and data["results"]
        meta = data.get("page_metadata", {})
        assert "hasNext" in meta
        assert all(r.get("generated_internal_id") for r in data["results"])
        # count endpoint (not page metadata) is the parity anchor.
        count = await guarded(c.post("search/spending_by_award_count/", {
            "filters": {"keywords": ["C-CURE 9000"],
                        "award_type_codes": ["A", "B", "C", "D"]}}))
        assert count.get("results", {}).get("contracts", 0) > 0
        # award_type_codes are mandatory: keyword search without them 422s.
        with pytest.raises(NonTransientError) as ei:
            await c.post("search/spending_by_award/", {
                "filters": {"keywords": ["C-CURE 9000"]},
                "fields": ["Award ID", "Award Amount"],
                "sort": "Award Amount", "order": "desc", "limit": 5, "page": 1})
        assert ei.value.status == 422
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_sort_must_be_in_fields_422():
    c = live_client()
    try:
        with pytest.raises(NonTransientError) as ei:
            await c.post("search/spending_by_award/", {
                "filters": {"keywords": ["C-CURE 9000"],
                            "award_type_codes": ["A", "B", "C", "D"]},
                "fields": ["Award ID"],
                "sort": "Award Amount", "order": "desc", "limit": 5, "page": 1})
        # sort field must be in fields: violation is 400, not 422.
        assert ei.value.status == 400
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_transaction_search_shape():
    c = live_client()
    try:
        data = await guarded(c.post("search/spending_by_transaction/", {
            "filters": {
                "time_period": [{"start_date": "2023-10-01",
                                 "end_date": "2026-12-31"}],
                "award_type_codes": ["A", "B", "C", "D"],
                "keywords": ["75D30124F00014"]},
            "fields": ["Award ID", "generated_internal_id", "Action Date",
                       "Transaction Amount"],
            "sort": "Transaction Amount", "order": "desc",
            "limit": 5, "page": 1}))
        rows = data.get("results", [])
        assert rows and all(r.get("generated_internal_id") for r in rows)
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_award_detail_shape():
    c = live_client()
    try:
        detail = await guarded(c.get(f"awards/{SIEMENS}/"))
        assert detail.get("piid") == "36C25521P0052"
        assert (detail.get("recipient") or {}).get("recipient_uei")
        assert detail.get("total_obligation") is not None
        assert (detail.get("period_of_performance") or {}).get("end_date")
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_idv_traversal_contract():
    c = live_client()
    try:
        detail = await guarded(c.get(f"awards/{CDC_JCI}/"))
        parent_gid = (detail.get("parent_award") or {}).get("generated_unique_award_id")
        assert parent_gid, "CDC JCI award must carry a parent GID"
        amounts = await guarded(c.get(f"idvs/amounts/{parent_gid}/"))
        assert isinstance(amounts, dict)
        kids = await guarded(c.post("idvs/awards/", {
            "award_id": parent_gid, "type": "child_awards", "limit": 5, "page": 1}))
        assert isinstance(kids.get("results"), list)
        act = await guarded(c.post("idvs/activity/", {
            "award_id": parent_gid, "limit": 5, "page": 1}))
        assert isinstance(act.get("results"), list)
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_subawards_and_recipient_shapes():
    c = live_client()
    try:
        subs = await guarded(c.post("subawards/", {"award_id": SIEMENS,
                                                   "limit": 5, "page": 1}))
        assert isinstance(subs.get("results"), list)
        # A UEI that is a corporate parent returns its children (list or
        # results-dict depending on result size — normalize both).
        raw_kids = await guarded(c.get("recipient/children/JZXZSN3BBL98/"))
        kids = raw_kids.get("results", []) if isinstance(raw_kids, dict) else raw_kids
        assert isinstance(kids, list) and kids
        assert all(k.get("uei") for k in kids if isinstance(k, dict))
        # A leaf recipient is not a parent: 400 with an explicit detail.
        with pytest.raises(NonTransientError) as ei:
            await c.get(f"recipient/children/{JCI_UEI}/")
        assert ei.value.status == 400
    finally:
        await c.aclose()
