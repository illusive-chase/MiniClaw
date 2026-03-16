"""Channel-agnostic logging setup (file + optional console)."""

import copy
import logging
from datetime import date
from pathlib import Path

from rich.console import Console, Theme
from rich.logging import RichHandler

# Silence noisy third-party loggers
logging.getLogger("markdown_it").setLevel(logging.WARNING)


class Truncatable:
    """A string wrapper: ``str()`` returns the truncated form, ``.full`` keeps the original."""

    __slots__ = ("full", "_short")

    def __init__(self, value: str, max_len: int = 256) -> None:
        self.full = value
        if len(value) <= max_len:
            self._short = value
        else:
            self._short = value[:max_len] + f"...({len(value)} total chars)"

    def __str__(self) -> str:
        return self._short

    def __repr__(self) -> str:
        return self._short


class _FullContentFormatter(logging.Formatter):
    """Formatter that resolves :class:`Truncatable` args to their full content."""

    def format(self, record: logging.LogRecord) -> str:
        record = copy.copy(record)
        if isinstance(record.args, tuple):
            record.args = tuple(
                a.full if isinstance(a, Truncatable) else a for a in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                k: v.full if isinstance(v, Truncatable) else v
                for k, v in record.args.items()
            }
        return super().format(record)


def setup_file_logging(file_level: int, workspace_dir: str) -> logging.FileHandler:
    """Install a FileHandler on the root logger.

    Log file is written to ``$workspace_dir/$date.log``.
    Returns the handler so callers can hold a reference if needed.
    """
    log_dir = Path(workspace_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{date.today().isoformat()}.log"

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(
        _FullContentFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    file_handler.setLevel(file_level)

    logging.root.addHandler(file_handler)
    logging.root.setLevel(file_level)

    return file_handler


def truncate(value: str, max_len: int = 256) -> "Truncatable":
    """Wrap a string for logging: truncated on console, full in file logs."""
    return Truncatable(value, max_len)


def adjust_root_level() -> None:
    """Set root logger level to the minimum of all installed handler levels."""
    handlers = logging.root.handlers
    if handlers:
        logging.root.setLevel(min(h.level for h in handlers))

_console = Console(theme=Theme({
    "markdown.code": "bold magenta on white",
    "markdown.code_block": "magenta on white",
    "markdown.hr": "gray70",
}))

def setup_console_logging(console_level: int) -> RichHandler:
    """Install a RichHandler on the root logger for console output.

    Returns the handler so callers can hold a reference if needed.
    """
    console_handler = RichHandler(
        console=_console,
        level=console_level,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
        show_time=True,
        show_path=True,
    )
    console_handler.setLevel(console_level)

    logging.root.addHandler(console_handler)
    adjust_root_level()

    return console_handler
