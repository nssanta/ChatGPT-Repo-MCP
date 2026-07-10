from __future__ import annotations

from typing import Literal, cast

from .server import mcp, settings


def main() -> None:
    transport = cast(Literal["stdio", "sse", "streamable-http"], settings.transport)
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
