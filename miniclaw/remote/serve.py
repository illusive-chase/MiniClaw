"""Entry point for ``minicode --serve`` — start RemoteDaemon."""

from __future__ import annotations

import asyncio
from typing import Any


def serve_main(
    config: dict[str, Any],
    host: str = "127.0.0.1",
    port: int = 9100,
) -> None:
    """Start the RemoteDaemon, blocking until Ctrl-C."""
    from miniclaw.remote.daemon import RemoteDaemon

    daemon = RemoteDaemon(config, host=host, port=port)
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass
