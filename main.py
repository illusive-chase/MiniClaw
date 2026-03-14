"""MiniClaw — minimal Python agent runtime."""

import argparse
import asyncio
import logging

from miniclaw.agent import Agent
from miniclaw.channels import create_channel
from miniclaw.config import load_config
from miniclaw.gateway import Gateway
from miniclaw.log import adjust_root_level, setup_file_logging
from miniclaw.memory import create_memory
from miniclaw.providers import create_provider
from miniclaw.session import SessionManager
from miniclaw.tools import create_registry

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

    # Phase 1: file-only logging (before channel creation)
    setup_file_logging(file_level=file_level, workspace_dir=workspace_dir)

    # Apply CLI overrides
    if args.channel:
        config["channel"]["type"] = args.channel
    if args.provider:
        config["provider"]["type"] = args.provider
    if args.model:
        config["provider"]["model"] = args.model

    # Inject console_level into channel config for CLIChannel to read
    config["channel"]["console_level"] = console_level

    # Build components
    provider = create_provider(config["provider"])
    memory = create_memory(config["memory"])
    tool_registry = create_registry(config, memory=memory)
    channel = create_channel(config["channel"])
    session_manager = SessionManager(workspace_dir)

    # Phase 2: register channel's log handler (if any)
    channel_handler = channel.log_handler()
    if channel_handler is not None:
        logging.root.addHandler(channel_handler)
        adjust_root_level()

    agent = Agent(
        provider=provider,
        tool_registry=tool_registry,
        memory=memory,
        system_prompt=config["agent"]["system_prompt"],
        max_tool_iterations=config["agent"]["max_tool_iterations"],
        default_model=config["provider"].get("model"),
        temperature=config["provider"].get("temperature", 0.7),
    )

    gateway = Gateway(agent=agent, session_manager=session_manager)
    gateway.register_channel(channel)

    logging.getLogger(__name__).info(
        f"Starting MiniClaw: provider={config['provider']['type']}, "
        f"channel={config['channel']['type']}, "
        f"tools={tool_registry.list_names()}")

    asyncio.run(gateway.run())


if __name__ == "__main__":
    main()
