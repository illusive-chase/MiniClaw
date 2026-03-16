"""Listener implementations."""

from __future__ import annotations

from miniclaw.agent.config import AgentConfig
from miniclaw.listeners.base import Listener

__all__ = ["Listener", "create_listener"]


def create_listener(
    config: dict,
    agent_type: str,
    agent_config: AgentConfig,
    workspace_dir: str,
) -> Listener:
    """Create a listener based on ``config["channel"]["type"]``."""
    channel_cfg = config.get("channel", {})
    channel_type = channel_cfg.get("type", "cli")

    if channel_type == "cli":
        from miniclaw.listeners.cli import CLIListener

        return CLIListener(
            agent_type=agent_type,
            agent_config=agent_config,
            workspace_dir=workspace_dir,
        )

    if channel_type == "feishu":
        from miniclaw.listeners.feishu import FeishuListener

        return FeishuListener(
            app_id=channel_cfg["app_id"],
            app_secret=channel_cfg["app_secret"],
            agent_type=agent_type,
            agent_config=agent_config,
        )

    raise ValueError(f"Unknown channel type: {channel_type!r}")
