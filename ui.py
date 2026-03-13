"""Shared Rich console and logging setup."""

import logging

from rich.console import Console
from rich.logging import RichHandler

console = Console()


def setup_rich_logging(level: int = logging.INFO) -> None:
    """Install RichHandler on the root logger."""
    handler = RichHandler(
        console=console,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
    )
    handler.setFormatter(logging.Formatter("%(message)s", datefmt="[%X]"))
    logging.root.handlers = [handler]
    logging.root.setLevel(level)
