"""Smoke tests for the C-SCRM MCP server."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_create_server_registers_discover_tool():
    os.environ["AUTH_ENABLED"] = "false"
    from usa_spending_mcp_server.server_http import create_server

    server = create_server()
    tools = await server.list_tools()
    tool_names = [t.name for t in tools]
    assert "discover_agencies_by_target" in tool_names
    assert "get_award_cscrm_details" in tool_names
    for name in (
        "search_awards_exhaustive",
        "traverse_idv_hierarchy",
        "resolve_recipient_tree",
        "fetch_award_full_context",
        "adjudicate_lexical_matches",
        "search_subawards_exhaustive",
        "run_cscrm_audit_suite",
    ):
        assert name in tool_names, f"missing tool: {name}"


def test_create_server_refuses_insecure_startup_when_auth_required():
    """With AUTH_ENABLED unset (default true) and no IdP config, startup fails."""
    import pytest

    from usa_spending_mcp_server.server_http import create_server

    env = {"AUTH_ENABLED": "true"}
    with patch.dict(os.environ, env, clear=True), pytest.raises((RuntimeError, ValueError)):
        create_server()


def test_auth_module_imports():
    from usa_spending_mcp_server import auth

    assert hasattr(auth, "create_logingov_auth")
