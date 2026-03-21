"""Statusline — user-customizable status display via external script."""

from __future__ import annotations

import asyncio
import json
import logging
import os

from miniclaw.types import UsageEvent

logger = logging.getLogger(__name__)


class StatusLineExecutor:
    """Runs a user-provided script to produce a single statusline string.

    The script receives JSON session data on stdin and should output one line
    on stdout. The result is cached and displayed during subsequent turns.
    """

    def __init__(self, command: str, timeout: float = 2.0) -> None:
        self._command = os.path.expanduser(command)
        self._timeout = timeout
        self._cached_text = ""

    @property
    def text(self) -> str:
        return self._cached_text

    async def refresh(self, data: dict) -> None:
        """Run the statusline script with *data* as JSON stdin."""
        try:
            proc = await asyncio.create_subprocess_shell(
                self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(json.dumps(data).encode()),
                timeout=self._timeout,
            )
            if proc.returncode == 0 and stdout:
                self._cached_text = stdout.decode().split("\n", 1)[0].strip()
            else:
                if stderr:
                    logger.warning("statusline script stderr: %s", stderr.decode()[:200])
                logger.warning(
                    "statusline script exited with code %s", proc.returncode
                )
        except asyncio.TimeoutError:
            logger.warning("statusline script timed out after %.1fs", self._timeout)
        except Exception:
            logger.warning("statusline script failed", exc_info=True)


def build_statusline_data(
    usage_event: UsageEvent,
    model: str,
    session_id: str,
) -> dict:
    """Build the JSON dict passed to the statusline script."""
    u = usage_event.usage
    ctx_tokens = usage_event.context_tokens or 0
    ctx_window = usage_event.context_window or 0
    pct = min(100, ctx_tokens * 100 // ctx_window) if ctx_window > 0 else 0

    last = usage_event.last_usage
    return {
        "model": {
            "id": model,
            "display_name": model,
        },
        "session_id": session_id,
        "cost": {
            "total_cost_usd": u.total_cost_usd,
            "total_duration_ms": u.total_duration_ms,
        },
        "usage": {
            "input_tokens": last.input_tokens if last else u.input_tokens,
            "output_tokens": last.output_tokens if last else u.output_tokens,
            "cache_read_tokens": last.cache_read_tokens if last else u.cache_read_tokens,
            "cache_creation_tokens": last.cache_creation_tokens if last else u.cache_creation_tokens,
            "total_input_tokens": u.input_tokens,
            "total_output_tokens": u.output_tokens,
        },
        "context_window": {
            "used_percentage": pct,
            "input_tokens": ctx_tokens,
            "context_window_size": ctx_window,
        },
    }
