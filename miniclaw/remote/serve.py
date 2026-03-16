"""Entry point for ``minicode --serve`` — start RemoteDaemon."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from miniclaw.config import load_config
from miniclaw.log import setup_console_logging, setup_file_logging


def serve_main(
    config: dict[str, Any],
    host: str = "0.0.0.0",
    port: int = 9100,
) -> None:
    """Start the RemoteDaemon, blocking until Ctrl-C."""
    from miniclaw.remote.daemon import RemoteDaemon

    daemon = RemoteDaemon(config, host=host, port=port)
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass
