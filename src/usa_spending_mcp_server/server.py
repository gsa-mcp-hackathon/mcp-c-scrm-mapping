"""stdio MCP server entry point."""

from __future__ import annotations

import asyncio
import logging

from fastmcp import FastMCP
from fastmcp.experimental.transforms.code_mode import CodeMode

from usa_spending_mcp_server.client import USASpendingClient
from usa_spending_mcp_server.tools import register_tools

logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="USASpendingCSCRM",
    list_page_size=50,
    transforms=[CodeMode()],
    instructions="MCP server for C-SCRM agency exposure reporting against the live USAspending.gov v2 API.",
)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(async_main())


async def async_main() -> None:
    async with USASpendingClient() as client:
        logger.info("Registering tools")
        register_tools(mcp, client)
        logger.info("Running USA Spending C-SCRM MCP Server (stdio)")
        await mcp.run_async()


if __name__ == "__main__":
    main()
