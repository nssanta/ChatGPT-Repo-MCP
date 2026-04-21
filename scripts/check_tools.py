from __future__ import annotations

import sys

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main(url: str) -> None:
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            print({"url": url, "count": len(names), "names": names})


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_tools.py https://YOUR.trycloudflare.com/mcp")
    anyio.run(main, sys.argv[1])
