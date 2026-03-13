"""MiniClaw — minimal Python agent runtime."""

import argparse
import asyncio
import logging

from miniclaw.agent import Agent
from miniclaw.channels import create_channel
from miniclaw.config import load_config
from miniclaw.memory import create_memory
from miniclaw.providers import create_provider
from miniclaw.session import SessionManager
from miniclaw.tools import create_registry
from miniclaw.ui import setup_rich_logging

_LOG_LEVEL_MAP = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def main():
    parser = argparse.ArgumentParser(description="MiniClaw runtime")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--channel",
        default=None,
        help="Override channel type (cli, feishu)",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="Override provider type (openai, anthropic)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override model name",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging (shortcut for --log-level debug)",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["debug", "info", "warning", "error"],
        help="Set console log level (overrides --verbose and config)",
    )
    args = parser.parse_args()

    # Load config
    config = load_config(args.config)

    # Resolve console log level: --log-level > --verbose > config > default
    log_cfg = config.get("logging", {})
    if args.log_level:
        console_level = _LOG_LEVEL_MAP[args.log_level]
    elif args.verbose:
        console_level = logging.DEBUG
    else:
        console_level = _LOG_LEVEL_MAP.get(
            str(log_cfg.get("console_level", "info")).lower(), logging.INFO
        )

    # Resolve file logging
    file_level = _LOG_LEVEL_MAP.get(
        str(log_cfg.get("file_level", "debug")).lower(), logging.DEBUG
    )

    workspace_dir = config["agent"].get("workspace_dir", ".workspace")

    logging_handles = setup_rich_logging(
        console_level=console_level,
        file_level=file_level,
        workspace_dir=workspace_dir,
    )

    # Apply CLI overrides
    if args.channel:
        config["channel"]["type"] = args.channel
    if args.provider:
        config["provider"]["type"] = args.provider
    if args.model:
        config["provider"]["model"] = args.model

    # Build components
    provider = create_provider(config["provider"])
    memory = create_memory(config["memory"])
    tool_registry = create_registry(config, memory=memory)
    channel = create_channel(config["channel"])

    session_manager = SessionManager(workspace_dir)

    agent = Agent(
        provider=provider,
        tool_registry=tool_registry,
        memory=memory,
        system_prompt=config["agent"]["system_prompt"],
        max_tool_iterations=config["agent"]["max_tool_iterations"],
        model=config["provider"].get("model"),
        temperature=config["provider"].get("temperature", 0.7),
        session_manager=session_manager,
    )

    # Wire channel to logging handles and agent commands
    if hasattr(channel, "bind_logging_handles"):
        channel.bind_logging_handles(logging_handles)
    if hasattr(channel, "register_agent_commands"):
        channel.register_agent_commands(agent.command_help())
    if hasattr(channel, "bind_session_manager"):
        channel.bind_session_manager(session_manager)
    if hasattr(channel, "bind_agent"):
        channel.bind_agent(agent)

    logging.getLogger(__name__).info(
        f"Starting MiniClaw: provider={config['provider']['type']}, "
        f"channel={config['channel']['type']}, "
        f"tools={tool_registry.list_names()}")

    asyncio.run(agent.run_channel(channel))


if __name__ == "__main__":
    main()
