"""Entry point for MiniClaw — native agent with tools."""

from __future__ import annotations

import asyncio
import logging

from miniclaw.agent.config import AgentConfig
from miniclaw.agent.native import NativeAgent
from miniclaw.config import load_config
from miniclaw.listeners import create_listener
from miniclaw.log import setup_console_logging, setup_file_logging
from miniclaw.persistence import SessionManager
from miniclaw.providers import create_provider
from miniclaw.runtime import Runtime
from miniclaw.tools import create_registry


def main() -> None:
    config = load_config()
    log_cfg = config.get("logging", {})
    file_level = getattr(logging, log_cfg.get("file_level", "warning").upper(), logging.WARNING)
    workspace_dir_cfg = config.get("agent", {}).get("workspace_dir", ".workspace")
    setup_file_logging(file_level, workspace_dir_cfg)

    console_level_str = log_cfg.get("console_level", "").upper()
    if console_level_str:
        console_level = getattr(logging, console_level_str, None)
        if console_level is not None:
            setup_console_logging(console_level)

    logger = logging.getLogger(__name__)
    logger.info("Starting MiniClaw (native agent)")

    # Build components
    agent_cfg = config.get("agent", {})
    workspace_dir = agent_cfg.get("workspace_dir", ".workspace")

    provider = create_provider(config["provider"])

    # Build agent config
    agent_config = AgentConfig(
        model=config["provider"].get("model", "gpt-4o"),
        system_prompt=agent_cfg.get("system_prompt", ""),
        max_iterations=agent_cfg.get("max_tool_iterations", 50),
        temperature=config["provider"].get("temperature", 0.7),
    )

    # Per-session factory: creates a fresh NativeAgent with RuntimeContext-aware registry
    def build_native_agent(cfg, runtime_context=None):
        registry = create_registry(config, runtime_context=runtime_context)
        return NativeAgent(
            provider=provider,
            tool_registry=registry,
            system_prompt=cfg.system_prompt or agent_config.system_prompt,
            default_model=cfg.model or agent_config.model,
            temperature=cfg.temperature or agent_config.temperature,
        )

    # CCAgent factory (if ccagent config is available)
    cc_cfg = config.get("ccagent", {})

    def build_ccagent(cfg, runtime_context=None):
        from miniclaw.agent.cc import CCAgent

        return CCAgent(
            system_prompt=cc_cfg.get("system_prompt", cfg.system_prompt or ""),
            default_model=cc_cfg.get("model", cfg.model or "claude-sonnet-4-6"),
            permission_mode=cc_cfg.get("permission_mode", "default"),
            allowed_tools=cc_cfg.get("allowed_tools"),
            cwd=cc_cfg.get("cwd"),
            max_turns=cc_cfg.get("max_turns"),
            thinking=cc_cfg.get("thinking"),
            effort=cc_cfg.get("effort"),
        )

    # Build runtime
    session_manager = SessionManager(workspace_dir)
    runtime = Runtime(session_manager)

    # Register agent factories
    runtime.register_agent("native", build_native_agent)
    runtime.register_agent("ccagent", build_ccagent)

    # Add listener (CLI or Feishu, based on config)
    listener = create_listener(config, agent_type="native", agent_config=agent_config, workspace_dir=workspace_dir)
    runtime.add_listener(listener)

    # Run
    try:
        asyncio.run(runtime.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
