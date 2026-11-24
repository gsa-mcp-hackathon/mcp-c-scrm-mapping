"""Phase 3c tests: global-count-verified IDV dedication (mocked transport).

No live calls. Covers: n=1 singleton, unknown-count no-traverse, broad
vehicle identity (detail- and amounts-sourced), expiry flag, and the 2-50
dedicated happy path.
"""

from datetime import date

import httpx
import pytest

from usaspending_cscrm_mcp.client import USAspendingClient
from usaspending_cscrm_mcp.pipeline.stage3_idv import (
    classify_container,
    traverse_vehicle,
)
from usaspending_cscrm_mcp.pipeline.stage4_adjudicate import term_re

TERM = "Zorpcore Widget"
TODAY = date(2026, 1, 1)


def keep(desc):
    return term_re(TERM).search(desc or "")


def vehicle_detail(piid="VG", desc="Zorpcore Widget bulk vehicle",
                   end="2027-09-30", potential=1000,
                   type_code: str | None = "IDV_D", type_desc: str | None = "BPA"):
    detail = {
        "piid": piid, "description": desc, "total_obligation": 500,
        "base_and_all_options": potential,
        "period_of_performance": {"start_date": "2023-10-01", "end_date": end},
    }
    if type_code is not None:
        detail["type"] = type_code
    if type_desc is not None:
        detail["type_description"] = type_desc
    return detail


def kid(gid, desc="Zorpcore Widget order"):
    return {"description": desc, "generated_unique_award_id": gid}


def mock_client(routes: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        import json as _j
        for (m, suffix), value in routes.items():
            if request.method == m and request.url.path.endswith(suffix):
                if callable(value):
                    resp = value(_j.loads(request.content.decode() or "{}"))
                    assert isinstance(resp, httpx.Response)
                    return resp
                assert isinstance(value, httpx.Response)
                return value
        return httpx.Response(404, json={"detail": "no route"})
    c = USAspendingClient()
    c.client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="https://api.usaspending.gov")
    return c


def tx_pages(*pages):
    def _h(body):
        page = int(body.get("page", 1))
        rows, total, has_next = pages[page - 1]
        return httpx.Response(200, json={"results": rows,
                                         "page_metadata": {"total": total,
                                                           "hasNext": has_next}})
    return _h


def routes_for(gid, detail, amounts, kids, activity):
    routes = {
        ("GET", f"awards/{gid}/"): httpx.Response(200, json=detail),
        ("POST", "idvs/awards/"): tx_pages((kids, len(kids), False)),
        ("POST", "idvs/activity/"): tx_pages((activity, len(activity), False)),
    }
    if isinstance(amounts, httpx.Response):
        routes[("GET", f"idvs/amounts/{gid}/")] = amounts
    else:
        routes[("GET", f"idvs/amounts/{gid}/")] = httpx.Response(200, json=amounts)
    return routes


@pytest.mark.asyncio
async def test_singleton_observed_n1():
    """Global n=1: child recorded exactly as enumerated, no DEDICATED label."""
    kids = [kid("GC1", "Zorpcore Widget renewal")]
    c = mock_client(routes_for(
        "VS", vehicle_detail(piid="VS", end="2027-09-30"),
        {"child_award_count": 1, "child_award_total_obligation": 500},
        kids, [{"recipient_id": "R1"}]))
    res = await traverse_vehicle(c, "VS", keep, today=TODAY)
    await c.aclose()
    assert res is not None
    assert res.classification == "SINGLETON_OBSERVED"
    assert res.classification_rule == "singleton-single-observed-child"
    assert res.global_child_count == 1
    assert res.child_count == 1 and res.expected_child_count == 1
    assert res.parity_ok is True
    assert res.keyword_matched_child_gids == ["GC1"]
    assert [e.child_gid for e in res.edges] == ["GC1"]
    assert res.ceiling_burn == pytest.approx(0.5)
    assert res.traversal == "singleton-observed-no-inference"
    assert res.traversal != "dedicated-children-enumerated"
    assert res.classification != "DEDICATED_SINGLE_PURPOSE_IDV"
    assert res.days_remaining is None
    assert res.expired_parent is False


@pytest.mark.asyncio
async def test_unknown_dedication_no_traverse():
    """Amounts unavailable: never DEDICATED, even for a dedicated-looking subset."""
    kids = [kid("GC1"), kid("GC2")]
    c = mock_client(routes_for(
        "VU", vehicle_detail(piid="VU"),
        httpx.Response(404, json={}),
        kids, [{"recipient_id": "R1"}, {"recipient_id": "R1"}]))
    res = await traverse_vehicle(c, "VU", keep, today=TODAY)
    await c.aclose()
    assert res is not None
    assert res.classification == "UNKNOWN_DEDICATION"
    assert res.classification_rule == "unknown-dedication-no-global-count"
    assert res.global_child_count is None
    assert res.expected_child_count is None
    assert res.parity_ok is None
    assert res.traversal == "skipped-unknown-dedication"
    assert res.tier2_inheritance_blocked is True
    # tools.py promotes only DEDICATED children: nothing hydrates from here.
    assert res.classification != "DEDICATED_SINGLE_PURPOSE_IDV"
    # Enumeration evidence is still retained for canary checks.
    assert len(res.edges) == 2


@pytest.mark.asyncio
async def test_broad_vehicle_identity_from_detail():
    """Verified GWAC type in award detail forces BROAD even at global n=1."""
    kids = [kid("GC1", "Zorpcore Widget renewal")]
    c = mock_client(routes_for(
        "VB", vehicle_detail(piid="VB", type_code="IDV_A", type_desc="GWAC"),
        {"child_award_count": 1, "child_award_total_obligation": 500},
        kids, [{"recipient_id": "R1"}]))
    res = await traverse_vehicle(c, "VB", keep, today=TODAY)
    await c.aclose()
    assert res is not None
    assert res.classification == "BROAD_MULTI_PURPOSE_IDV"
    assert res.classification_rule == "broad-verified-vehicle-type"
    assert res.vehicle_identity == "BROAD_VERIFIED"
    assert res.vehicle_type_source == "local"
    assert res.global_child_count == 1
    assert res.traversal == "skipped-broad-vehicle"
    assert res.days_remaining is None


@pytest.mark.asyncio
async def test_broad_vehicle_identity_from_amounts():
    """Broad type carried only by the amounts record: source 'global'."""
    kids = [kid("GC1"), kid("GC2"), kid("GC3")]
    c = mock_client(routes_for(
        "VG", vehicle_detail(piid="VG", type_code=None, type_desc=None),
        {"child_award_count": 3, "child_award_total_obligation": 900,
         "type_description": "MULTI-AGENCY CONTRACT"},
        kids, [{"recipient_id": "R1"}] * 3))
    res = await traverse_vehicle(c, "VG", keep, today=TODAY)
    await c.aclose()
    assert res is not None
    assert res.classification == "BROAD_MULTI_PURPOSE_IDV"
    assert res.classification_rule == "broad-verified-vehicle-type"
    assert res.vehicle_identity == "BROAD_VERIFIED"
    assert res.vehicle_type_source == "global"


@pytest.mark.asyncio
async def test_expiry_flag():
    """expired_parent is True exactly when parent PoP end is before today."""
    kids = [kid("GC1"), kid("GC2"), kid("GC3")]
    amounts = {"child_award_count": 3, "child_award_total_obligation": 400}
    c = mock_client(routes_for(
        "VE", vehicle_detail(piid="VE", end="2014-11-07"),
        amounts, kids, [{"recipient_id": "R1"}] * 3))
    old = await traverse_vehicle(c, "VE", keep, today=TODAY)
    await c.aclose()
    assert old is not None
    assert old.classification == "DEDICATED_SINGLE_PURPOSE_IDV"
    assert old.expired_parent is True
    assert old.days_remaining == (date(2014, 11, 7) - TODAY).days
    assert old.days_remaining is not None and old.days_remaining < 0

    c2 = mock_client(routes_for(
        "VF", vehicle_detail(piid="VF", end="2027-09-30"),
        amounts, kids, [{"recipient_id": "R1"}] * 3))
    new = await traverse_vehicle(c2, "VF", keep, today=TODAY)
    await c2.aclose()
    assert new is not None
    assert new.expired_parent is False
    assert new.days_remaining is not None and new.days_remaining > 0


@pytest.mark.asyncio
async def test_dedicated_happy_path_2_to_50():
    """Global n in [2, 50] + single awardee + scope match: DEDICATED verified."""
    kids = [kid("GC1"), kid("GC2"), kid("GC3")]
    c = mock_client(routes_for(
        "VD", vehicle_detail(piid="VD", end="2027-09-30", potential=1000),
        {"child_award_count": 3, "child_award_total_obligation": 400},
        kids, [{"recipient_id": "R1"}] * 3))
    res = await traverse_vehicle(c, "VD", keep, today=TODAY)
    await c.aclose()
    assert res is not None
    assert res.classification == "DEDICATED_SINGLE_PURPOSE_IDV"
    assert res.classification_rule == "gate-A-description"
    assert res.global_child_count == 3
    assert res.parity_ok is True and res.child_count == res.expected_child_count == 3
    assert res.ceiling_burn == pytest.approx(0.4)
    assert res.traversal == "dedicated-children-enumerated"
    assert res.vehicle_identity == "DEDICATED_VERIFIED"
    assert res.vehicle_type_source == "local"
    assert res.expired_parent is False

    # Same counts but no live type fields: still DEDICATED, identity UNKNOWN.
    c2 = mock_client(routes_for(
        "VDN", vehicle_detail(piid="VDN", type_code=None, type_desc=None),
        {"child_award_count": 2, "child_award_total_obligation": 100},
        kids[:2], [{"recipient_id": "R1"}] * 2))
    res2 = await traverse_vehicle(c2, "VDN", keep, today=TODAY)
    await c2.aclose()
    assert res2 is not None
    assert res2.classification == "DEDICATED_SINGLE_PURPOSE_IDV"
    assert res2.vehicle_identity == "UNKNOWN"
    assert res2.global_child_count == 2


def test_classify_container_global_routing():
    assert classify_container(1, False, True, 1.0, 1,
                              global_child_count=1)[0] == "SINGLETON_OBSERVED"
    assert classify_container(2, True, True, 1.0, 1,
                              global_child_count=None)[0] == "UNKNOWN_DEDICATION"
    assert classify_container(2, True, True, 1.0, 1,
                              global_child_count=3)[0] == "DEDICATED_SINGLE_PURPOSE_IDV"
    assert classify_container(2, True, True, 1.0, 1, global_child_count=3,
                              broad_vehicle_type=True)[0] == "BROAD_MULTI_PURPOSE_IDV"
    assert classify_container(2, True, True, 1.0, 1,
                              global_child_count=60)[0] == "BROAD_MULTI_PURPOSE_IDV"
    # Legacy direct-call compatibility (no global count passed).
    assert classify_container(10, True, True, 0.5, 1)[0] == "DEDICATED_SINGLE_PURPOSE_IDV"
