"""Mini-Agent — minimal Python agent runtime."""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Ensure the mini-agent directory is on the Python path
sys.path.insert(0, str(Path(__file__).parent))

from agent import Agent
from channels import create_channel
from config import load_config
from memory import create_memory
from providers import create_provider
from tools import create_registry


def main():
    parser = argparse.ArgumentParser(description="Mini-Agent runtime")
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
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Load config
    config = load_config(args.config)

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

    agent = Agent(
        provider=provider,
        tool_registry=tool_registry,
        memory=memory,
        system_prompt=config["agent"]["system_prompt"],
        max_tool_iterations=config["agent"]["max_tool_iterations"],
        model=config["provider"].get("model"),
        temperature=config["provider"].get("temperature", 0.7),
    )

    logging.getLogger(__name__).info(
        f"Starting mini-agent: provider={config['provider']['type']}, "
        f"channel={config['channel']['type']}, "
        f"tools={tool_registry.list_names()}")

    asyncio.run(agent.run_channel(channel))


if __name__ == "__main__":
    main()
