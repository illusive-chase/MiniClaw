"""Shared Rich console and logging setup."""

import logging
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

console = Console(theme=Theme({
    "markdown.code": "bold magenta on white",
    "markdown.code_block": "magenta on white",
    "markdown.hr": "gray70",
}))


@dataclass
class LoggingHandles:
    """Handles to logging handlers for runtime level adjustment."""

    console_handler: RichHandler
    file_handler: logging.FileHandler | None = None


def setup_rich_logging(
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
    workspace_dir: str = ".",
) -> LoggingHandles:
    """Install RichHandler and FileHandler on the root logger.

    Log file is always written to ``$workspace/$date.log``.
    """
    from datetime import date

    console_handler = RichHandler(
        console=console,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
    )
    console_handler.setFormatter(logging.Formatter("%(message)s", datefmt="[%X]"))
    console_handler.setLevel(console_level)

    logging.root.handlers = [console_handler]

    log_dir = Path(workspace_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{date.today().isoformat()}.log"

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    file_handler.setLevel(file_level)
    logging.root.addHandler(file_handler)
    logging.root.setLevel(min(console_level, file_level))

    return LoggingHandles(console_handler=console_handler, file_handler=file_handler)
