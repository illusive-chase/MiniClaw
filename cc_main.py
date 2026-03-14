"""MiniClaw — Claude Code agent runtime entry point."""

import argparse
import asyncio
import logging

from miniclaw.ccagent import CCAgent
from miniclaw.channels import create_channel
from miniclaw.config import load_config
from miniclaw.gateway import Gateway
from miniclaw.log import adjust_root_level, setup_file_logging
from miniclaw.session import SessionManager

_LOG_LEVEL_MAP = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def main():
    parser = argparse.ArgumentParser(description="MiniClaw CC runtime")
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
    parser.add_argument(
        "--permission-mode",
        default=None,
        help="SDK permission mode (default, plan, acceptEdits, bypassPermissions)",
    )
    parser.add_argument(
        "--allowed-tools",
        default=None,
        help="Comma-separated SDK tool names to allow",
    )
    parser.add_argument(
        "--max-turns",
        default=None,
        type=int,
        help="Maximum agent turns before stopping",
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

    # Workspace dir — fall back to agent section, then default
    workspace_dir = config.get("agent", {}).get("workspace_dir", ".workspace")

    # Phase 1: file-only logging (before channel creation)
    setup_file_logging(file_level=file_level, workspace_dir=workspace_dir)

    # Apply CLI overrides
    if args.channel:
        config["channel"]["type"] = args.channel

    # Inject console_level into channel config for CLIChannel to read
    config["channel"]["console_level"] = console_level

    # Resolve CCAgent config: ccagent section with fallbacks to agent/provider
    cc_cfg = config.get("ccagent", {})
    system_prompt = cc_cfg.get("system_prompt") or config.get("agent", {}).get("system_prompt", "")
    model = args.model or cc_cfg.get("model") or config.get("provider", {}).get("model")
    permission_mode = args.permission_mode or cc_cfg.get("permission_mode", "default")
    cwd = cc_cfg.get("cwd")
    max_turns = args.max_turns or cc_cfg.get("max_turns")

    allowed_tools = None
    if args.allowed_tools:
        allowed_tools = [t.strip() for t in args.allowed_tools.split(",") if t.strip()]
    elif cc_cfg.get("allowed_tools"):
        allowed_tools = cc_cfg["allowed_tools"]

    # Build components
    channel = create_channel(config["channel"])
    session_manager = SessionManager(workspace_dir)

    # Phase 2: register channel's log handler (if any)
    channel_handler = channel.log_handler()
    if channel_handler is not None:
        logging.root.addHandler(channel_handler)
        adjust_root_level()

    agent = CCAgent(
        system_prompt=system_prompt,
        default_model=model,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        cwd=cwd,
        max_turns=max_turns,
    )

    gateway = Gateway(agent=agent, session_manager=session_manager)
    gateway.register_channel(channel)

    logging.getLogger(__name__).info(
        "Starting MiniClaw (CC): model=%s, channel=%s, permission_mode=%s",
        model,
        config["channel"]["type"],
        permission_mode,
    )

    asyncio.run(gateway.run())


if __name__ == "__main__":
    main()
