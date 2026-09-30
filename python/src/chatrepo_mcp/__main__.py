from __future__ import annotations

from typing import Literal, cast

from .maintenance import start_runtime_maintenance, stop_runtime_maintenance
from .server import mcp, settings


def main() -> None:
    transport = cast(Literal["stdio", "sse", "streamable-http"], settings.transport)
    start_runtime_maintenance(settings)
    try:
        mcp.run(transport=transport)
    finally:
        stop_runtime_maintenance()


if __name__ == "__main__":
    main()
