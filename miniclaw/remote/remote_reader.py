"""RemoteReader — persistent WebSocket connection for remote file operations.

Used by tools to transparently read files from a remote workspace via daemon RPCs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

from miniclaw.remote.protocol import serialize_file_glob, serialize_file_grep, serialize_file_read

logger = logging.getLogger(__name__)


class RemoteReader:
    """Persistent connection to a remote daemon for file operations."""

    def __init__(self) -> None:
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def connect(self, ws_url: str) -> None:
        """Connect to the remote daemon's file operation endpoint."""
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(ws_url)
        logger.info("[RemoteReader] Connected to %s", ws_url)

    async def file_read(self, path: str) -> str:
        """Read a file from the remote daemon."""
        return await self._rpc(serialize_file_read(path), "file_read_result", "content")

    async def glob(self, path: str, pattern: str) -> list[str]:
        """Glob files on the remote daemon."""
        return await self._rpc(serialize_file_glob(path, pattern), "file_glob_result", "matches")

    async def grep(self, path: str, pattern: str, file_glob: str = "") -> list[str]:
        """Grep files on the remote daemon."""
        return await self._rpc(serialize_file_grep(path, pattern, file_glob), "file_grep_result", "matches")

    async def _rpc(self, msg: dict[str, Any], expected_type: str, result_key: str) -> Any:
        """Send an RPC message and wait for the response."""
        async with self._lock:
            if self._ws is None or self._ws.closed:
                raise ConnectionError("RemoteReader not connected")
            await self._ws.send_json(msg)
            resp = await self._ws.receive_json()
            if resp.get("type") != expected_type:
                error = resp.get("error", f"Unexpected response type: {resp.get('type')}")
                raise RuntimeError(error)
            if not resp.get("ok", False):
                raise RuntimeError(resp.get("error", "Remote operation failed"))
            return resp.get(result_key)

    async def close(self) -> None:
        """Close the connection."""
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session:
            await self._session.close()
        self._ws = None
        self._session = None
        logger.info("[RemoteReader] Connection closed")
