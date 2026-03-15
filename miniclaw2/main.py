"""Entry point for MiniClaw v2 — native agent with tools."""

from __future__ import annotations

import asyncio
import logging

from miniclaw.config import load_config
from miniclaw.log import setup_file_logging
from miniclaw.memory import create_memory
from miniclaw.providers import create_provider
from miniclaw.session import SessionManager
from miniclaw.tools import create_registry
from miniclaw2.agent.config import AgentConfig
from miniclaw2.agent.native import NativeAgent
from miniclaw2.listeners.cli import CLIListener
from miniclaw2.runtime import Runtime


def main() -> None:
    config = load_config()
    log_cfg = config.get("logging", {})
    file_level = getattr(logging, log_cfg.get("file_level", "warning").upper(), logging.WARNING)
    workspace_dir_cfg = config.get("agent", {}).get("workspace_dir", ".workspace")
    setup_file_logging(file_level, workspace_dir_cfg)

    logger = logging.getLogger(__name__)
    logger.info("Starting MiniClaw v2 (native agent)")

    # Build components
    agent_cfg = config.get("agent", {})
    workspace_dir = agent_cfg.get("workspace_dir", ".workspace")

    provider = create_provider(config["provider"])
    memory = create_memory(config.get("memory", {}), workspace_dir)
    tool_registry = create_registry(config, memory)

    # Build agent config
    agent_config = AgentConfig(
        model=config["provider"].get("model", "gpt-4o"),
        system_prompt=agent_cfg.get("system_prompt", ""),
        max_iterations=agent_cfg.get("max_tool_iterations", 15),
        temperature=config["provider"].get("temperature", 0.7),
        memory_enabled=True,
    )

    # Create native agent
    native_agent = NativeAgent(
        provider=provider,
        tool_registry=tool_registry,
        memory=memory,
        system_prompt=agent_config.system_prompt,
        default_model=agent_config.model,
        temperature=agent_config.temperature,
    )

    # Build runtime
    session_manager = SessionManager(workspace_dir)
    runtime = Runtime(session_manager)

    # Register agent factory
    runtime.register_agent("native", lambda cfg: native_agent)

    # Add CLI listener
    cli_listener = CLIListener(
        agent_type="native",
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
