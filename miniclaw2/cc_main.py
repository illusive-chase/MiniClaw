"""Entry point for MiniClaw v2 — CCAgent with Claude Agent SDK."""

from __future__ import annotations

import asyncio
import logging

from miniclaw.config import load_config
from miniclaw.log import setup_file_logging
from miniclaw.session import SessionManager
from miniclaw2.agent.cc import CCAgent
from miniclaw2.agent.config import AgentConfig
from miniclaw2.listeners.cli import CLIListener
from miniclaw2.runtime import Runtime


def main() -> None:
    config = load_config()
    log_cfg = config.get("logging", {})
    file_level = getattr(logging, log_cfg.get("file_level", "warning").upper(), logging.WARNING)
    workspace_dir_cfg = config.get("agent", {}).get("workspace_dir", ".workspace")
    setup_file_logging(file_level, workspace_dir_cfg)

    logger = logging.getLogger(__name__)
    logger.info("Starting MiniClaw v2 (CCAgent)")

    cc_cfg = config.get("ccagent", {})
    workspace_dir = config.get("agent", {}).get("workspace_dir", ".workspace")

    # Create CCAgent
    cc_agent = CCAgent(
        system_prompt=cc_cfg.get("system_prompt", ""),
        default_model=cc_cfg.get("model", "claude-sonnet-4-6"),
        permission_mode=cc_cfg.get("permission_mode", "default"),
        allowed_tools=cc_cfg.get("allowed_tools"),
        cwd=cc_cfg.get("cwd"),
        max_turns=cc_cfg.get("max_turns"),
        thinking=cc_cfg.get("thinking"),
        effort=cc_cfg.get("effort"),
    )

    # Build agent config
    agent_config = AgentConfig(
        model=cc_cfg.get("model", "claude-sonnet-4-6"),
        system_prompt=cc_cfg.get("system_prompt", ""),
        thinking=cc_cfg.get("thinking") is not None,
        effort=cc_cfg.get("effort", "medium"),
    )

    # Build runtime
    session_manager = SessionManager(workspace_dir)
    runtime = Runtime(session_manager)

    # Register agent factory
    runtime.register_agent("ccagent", lambda cfg: cc_agent)

    # Add CLI listener
    cli_listener = CLIListener(
        agent_type="ccagent",
        agent_config=agent_config,
        workspace_dir=workspace_dir,
    )
    runtime.add_listener(cli_listener)

    # Run
    try:
        asyncio.run(runtime.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
