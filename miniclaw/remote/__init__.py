"""Remote CCAgent execution via WebSocket.

Exports:
    RemoteSubAgentDriver — local-side WS client (replaces SubAgentDriver for remote)
    RemoteDaemon         — WS server running on the remote machine
    serve_main           — entry point for ``minicode --serve``
    SSHTunnel            — single SSH tunnel subprocess manager
    TunnelManager        — pool of SSH tunnels keyed by config name
    TunnelError          — raised on tunnel creation failure
"""

from miniclaw.remote.remote_driver import RemoteSubAgentDriver
from miniclaw.remote.daemon import RemoteDaemon
from miniclaw.remote.serve import serve_main
from miniclaw.remote.tunnel import SSHTunnel, TunnelManager, TunnelError

__all__ = [
    "RemoteSubAgentDriver",
    "RemoteDaemon",
    "serve_main",
    "SSHTunnel",
    "TunnelManager",
    "TunnelError",
]
