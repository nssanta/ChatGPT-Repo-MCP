from __future__ import annotations

from .server import mcp, settings


def main() -> None:
    mcp.run(transport=settings.transport, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
