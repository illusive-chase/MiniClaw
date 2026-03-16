"""SSH tunnel manager for secure remote agent connections.

Provides ``SSHTunnel`` (single tunnel subprocess) and ``TunnelManager``
(pool of tunnels keyed by remote config name).  Used by RuntimeContext
to transparently forward local WebSocket connections through SSH to a
RemoteDaemon bound on ``127.0.0.1``.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

logger = logging.getLogger(__name__)


class TunnelError(Exception):
    """Raised when an SSH tunnel cannot be established."""


class SSHTunnel:
    """Manages a single SSH tunnel subprocess.

    Uses the system ``ssh`` binary so that ``~/.ssh/config`` and
    ``ssh-agent`` are honoured automatically.
    """

    def __init__(
        self,
        ssh_host: str,
        remote_port: int = 9100,
        local_port: int = 0,
        ssh_user: str | None = None,
        ssh_port: int = 22,
        ssh_key: str | None = None,
    ) -> None:
        self._ssh_host = ssh_host
        self._remote_port = remote_port
        self._local_port = local_port
        self._ssh_user = ssh_user
        self._ssh_port = ssh_port
        self._ssh_key = ssh_key
        self._process: asyncio.subprocess.Process | None = None

    async def start(self) -> int:
        """Launch the SSH tunnel and return the actual local port."""
        if self._local_port == 0:
            self._local_port = self._find_free_port()

        destination = (
            f"{self._ssh_user}@{self._ssh_host}"
            if self._ssh_user
            else self._ssh_host
        )

        cmd = [
            "ssh", "-N",
            "-L", f"{self._local_port}:127.0.0.1:{self._remote_port}",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-p", str(self._ssh_port),
        ]
        if self._ssh_key:
            cmd.extend(["-i", self._ssh_key])
        cmd.append(destination)

        logger.info("Starting SSH tunnel: %s", " ".join(cmd))

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        # Wait briefly for the process to either stabilise or fail.
        try:
            await asyncio.wait_for(self._process.wait(), timeout=5.0)
            # If we reach here the process exited within 5s — that's a failure.
            stderr = b""
            if self._process.stderr:
                stderr = await self._process.stderr.read()
            raise TunnelError(
                f"SSH tunnel exited immediately (rc={self._process.returncode}): "
                f"{stderr.decode(errors='replace').strip()}"
            )
        except asyncio.TimeoutError:
            # Process is still running after 5s — tunnel is up.
            pass

        logger.info(
            "SSH tunnel established: local port %d -> %s:%d",
            self._local_port, self._ssh_host, self._remote_port,
        )
        return self._local_port

    async def close(self) -> None:
        """Terminate the SSH tunnel process."""
        if self._process is None:
            return
        if self._process.returncode is not None:
            # Already exited.
            self._process = None
            return
        self._process.terminate()
        try:
            await asyncio.wait_for(self._process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            self._process.kill()
            await self._process.wait()
        logger.info("SSH tunnel closed (local port %d)", self._local_port)
        self._process = None

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def local_port(self) -> int:
        return self._local_port

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self._local_port}/ws"

    @staticmethod
    def _find_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


class TunnelManager:
    """Pool of SSH tunnels keyed by remote config name.

    Reuses live tunnels — multiple sessions targeting the same remote
    share a single SSH connection.  Dead tunnels are replaced
    transparently.
    """

    def __init__(self) -> None:
        self._tunnels: dict[str, SSHTunnel] = {}

    async def get_or_create(self, key: str, config: dict[str, Any]) -> SSHTunnel:
        """Return an existing live tunnel or create a new one.

        ``config`` is a dict with keys matching the ``remotes`` YAML
        schema (``ssh_host``, ``ssh_user``, ``ssh_port``, ``ssh_key``,
        ``daemon_port``, ``local_port``).
        """
        tunnel = self._tunnels.get(key)
        if tunnel is not None and tunnel.is_alive:
            return tunnel

        # Existing tunnel is dead — discard and recreate.
        if tunnel is not None:
            await tunnel.close()

        tunnel = SSHTunnel(
            ssh_host=config["ssh_host"],
            remote_port=config.get("daemon_port", 9100),
            local_port=config.get("local_port", 0),
            ssh_user=config.get("ssh_user"),
            ssh_port=config.get("ssh_port", 22),
            ssh_key=config.get("ssh_key"),
        )
        await tunnel.start()
        self._tunnels[key] = tunnel
        return tunnel

    async def close(self, key: str) -> None:
        """Close and remove a specific tunnel."""
        tunnel = self._tunnels.pop(key, None)
        if tunnel is not None:
            await tunnel.close()

    async def close_all(self) -> None:
        """Close all managed tunnels."""
        for tunnel in self._tunnels.values():
            try:
                await tunnel.close()
            except Exception as e:
                logger.error("Error closing tunnel: %s", e)
        self._tunnels.clear()
