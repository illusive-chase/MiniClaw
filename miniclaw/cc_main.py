"""Entry point for MiniClaw — CCAgent with Claude Agent SDK."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from miniclaw.config import load_config
from miniclaw.log import setup_console_logging, setup_file_logging
from miniclaw.persistence import SessionManager
from miniclaw.agent.cc import CCAgent
from miniclaw.agent.config import AgentConfig
from miniclaw.listeners import create_listener
from miniclaw.runtime import Runtime


def main() -> None:
    parser = argparse.ArgumentParser(description="MiniClaw — CCAgent")
    parser.add_argument("--serve", action="store_true", help="Run as remote daemon")
    parser.add_argument("--host", default="127.0.0.1", help="Daemon bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9100, help="Daemon bind port (default: 9100)")
    args = parser.parse_args()

    config = load_config()
    log_cfg = config.get("logging", {})
    file_level = getattr(logging, log_cfg.get("file_level", "warning").upper(), logging.WARNING)
    workspace_dir_cfg = config["agent"]["workspace_dir"]
    setup_file_logging(file_level, workspace_dir_cfg)

    console_level_str = log_cfg.get("console_level", "").upper()
    if console_level_str:
        console_level = getattr(logging, console_level_str, None)
        if console_level is not None:
            setup_console_logging(console_level)

    logger = logging.getLogger(__name__)

    # --serve mode: start RemoteDaemon and return
    if args.serve:
        logger.info("Starting MiniClaw RemoteDaemon on %s:%d", args.host, args.port)
        from miniclaw.remote.serve import serve_main
        serve_main(config, host=args.host, port=args.port)
        return

    logger.info("Starting MiniClaw (CCAgent)")

    cc_cfg = config.get("ccagent", {})
    workspace_dir = config["agent"]["workspace_dir"]

    # Build agent config
    agent_config = AgentConfig(
        model=cc_cfg.get("model", "claude-sonnet-4-6"),
        system_prompt=cc_cfg.get("system_prompt", ""),
        thinking=cc_cfg.get("thinking") is not None,
        effort=cc_cfg.get("effort", "medium"),
    )

    # CCAgent factory (per-session)
    def build_ccagent(cfg, runtime_context=None):
        return CCAgent(
            system_prompt=cc_cfg.get("system_prompt", cfg.system_prompt or ""),
            default_model=cc_cfg.get("model", cfg.model or "claude-sonnet-4-6"),
            permission_mode=cc_cfg.get("permission_mode", "default"),
            allowed_tools=cc_cfg.get("allowed_tools"),
            cwd=cc_cfg.get("cwd") or os.getcwd(),
            max_turns=cc_cfg.get("max_turns"),
            thinking=cc_cfg.get("thinking"),
            effort=cc_cfg.get("effort"),
            context_window=config.get("provider", {}).get("context_window", 0),
        )

    # NativeAgent factory (for sub-agents or other use)
    def build_native_agent(cfg, runtime_context=None):
        from miniclaw.agent.native import NativeAgent
        from miniclaw.providers import create_provider
        from miniclaw.tools import create_registry

        provider = create_provider(config["provider"])
        registry = create_registry(config, runtime_context=runtime_context)
        return NativeAgent(
            provider=provider,
            tool_registry=registry,
            system_prompt=cfg.system_prompt or "",
            default_model=cfg.model or config["provider"].get("model", ""),
            temperature=cfg.temperature or 0.7,
            context_window=config["provider"].get("context_window", 0),
        )

    # Build runtime
    remotes_config = config.get("remotes", {})
    session_manager = SessionManager(workspace_dir)
    runtime = Runtime(
        session_manager,
        plugctx_config=config.get("plugctx"),
        remotes_config=remotes_config or None,
    )

    # Register agent factories
    runtime.register_agent("ccagent", build_ccagent)
    runtime.register_agent("native", build_native_agent)

    # Add listener (CLI or Feishu, based on config)
    listener = create_listener(config, agent_type="ccagent", agent_config=agent_config, workspace_dir=workspace_dir)
    runtime.add_listener(listener)

    # Run
    try:
        asyncio.run(runtime.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
