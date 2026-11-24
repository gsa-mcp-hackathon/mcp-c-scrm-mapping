"""Tool registration aggregator."""

from __future__ import annotations

from fastmcp import FastMCP

from usa_spending_mcp_server.client import USASpendingClient
from usa_spending_mcp_server.tools.cscrm_agency_exposure import register_cscrm_tools
from usa_spending_mcp_server.tools.cscrm_audit import register_run_cscrm_audit_suite
from usa_spending_mcp_server.tools.cscrm_award_details import register_cscrm_award_details
from usa_spending_mcp_server.tools.cscrm_context import register_fetch_award_full_context
from usa_spending_mcp_server.tools.cscrm_idv import register_traverse_idv_hierarchy
from usa_spending_mcp_server.tools.cscrm_lexicon import register_adjudicate_lexical_matches
from usa_spending_mcp_server.tools.cscrm_recipient import register_resolve_recipient_tree
from usa_spending_mcp_server.tools.cscrm_search import register_search_awards_exhaustive
from usa_spending_mcp_server.tools.cscrm_subawards import register_search_subawards_exhaustive
from usa_spending_mcp_server.tools.cscrm_triage import register_triage_and_deconflict_target


def register_tools(mcp: FastMCP, client: USASpendingClient) -> None:
    register_cscrm_tools(mcp, client)
    register_cscrm_award_details(mcp, client)
    register_search_awards_exhaustive(mcp, client)
    register_traverse_idv_hierarchy(mcp, client)
    register_resolve_recipient_tree(mcp, client)
    register_fetch_award_full_context(mcp, client)
    register_adjudicate_lexical_matches(mcp, client)
    register_search_subawards_exhaustive(mcp, client)
    register_triage_and_deconflict_target(mcp, client)
    register_run_cscrm_audit_suite(mcp, client)
