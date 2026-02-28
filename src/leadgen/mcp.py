"""
LeadGen MCP server entry point for Claude Desktop.

Usage:
    python -m leadgen.mcp

Add to Claude Desktop config:
    "leadgen": {
      "command": "python",
      "args": ["-m", "leadgen.mcp"]
    }
"""

import asyncio
import logging

from leadgen.mcp_server.server import main as mcp_main

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(mcp_main())
