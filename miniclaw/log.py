"""Channel-agnostic file-only logging setup."""

import logging
from datetime import date
from pathlib import Path


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
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    file_handler.setLevel(file_level)

    logging.root.addHandler(file_handler)
    logging.root.setLevel(file_level)

    return file_handler


def adjust_root_level() -> None:
    """Set root logger level to the minimum of all installed handler levels."""
    handlers = logging.root.handlers
    if handlers:
        logging.root.setLevel(min(h.level for h in handlers))
