from __future__ import annotations

import threading
from typing import Literal, cast
from types import FrameType

import anyio
import uvicorn

from .command_tools import shutdown_command_jobs
from .maintenance import start_runtime_maintenance, stop_runtime_maintenance
from .operations import registry_for
from .server import mcp, settings
from .terminal_tools import shutdown_terminal_sessions


class OperationAwareHTTPServer(uvicorn.Server):
    """Signal cancellation before Uvicorn waits for active HTTP requests."""

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if not self.should_exit:
            # A signal can interrupt audit/registry code; never take those locks
            # synchronously from the interrupted event-loop thread.
            threading.Thread(
                target=registry_for(settings).shutdown, daemon=True, name="operation-shutdown"
            ).start()
        super().handle_exit(sig, frame)


async def run_http(transport: str) -> None:
    app = mcp.sse_app() if transport == "sse" else mcp.streamable_http_app()
    config = uvicorn.Config(
        app,
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
    )
    await OperationAwareHTTPServer(config).serve()


def main() -> None:
    transport = cast(Literal["stdio", "sse", "streamable-http"], settings.transport)
    start_runtime_maintenance(settings)
    try:
        if transport == "stdio":
            mcp.run(transport=transport)
        else:
            anyio.run(run_http, transport)
    finally:
        registry_for(settings).shutdown()
        shutdown_command_jobs(settings)
        shutdown_terminal_sessions(settings)
        stop_runtime_maintenance()


if __name__ == "__main__":
    main()
