"""Entry point for MiniClaw — CCAgent with Claude Agent SDK."""

from __future__ import annotations

import asyncio
import logging

from miniclaw.config import load_config
from miniclaw.log import setup_file_logging
from miniclaw.persistence import SessionManager
from miniclaw.agent.cc import CCAgent
from miniclaw.agent.config import AgentConfig
from miniclaw.listeners.cli import CLIListener
from miniclaw.runtime import Runtime


def main() -> None:
    config = load_config()
    log_cfg = config.get("logging", {})
    file_level = getattr(logging, log_cfg.get("file_level", "warning").upper(), logging.WARNING)
    workspace_dir_cfg = config.get("agent", {}).get("workspace_dir", ".workspace")
    setup_file_logging(file_level, workspace_dir_cfg)

    logger = logging.getLogger(__name__)
    logger.info("Starting MiniClaw (CCAgent)")

    cc_cfg = config.get("ccagent", {})
    workspace_dir = config.get("agent", {}).get("workspace_dir", ".workspace")

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
            cwd=cc_cfg.get("cwd"),
            max_turns=cc_cfg.get("max_turns"),
            thinking=cc_cfg.get("thinking"),
            effort=cc_cfg.get("effort"),
        )

    # NativeAgent factory (for sub-agents or other use)
    def build_native_agent(cfg, runtime_context=None):
        from miniclaw.agent.native import NativeAgent
        from miniclaw.memory import create_memory
        from miniclaw.providers import create_provider
        from miniclaw.tools import create_registry

        provider = create_provider(config["provider"])
        memory = create_memory(config.get("memory", {}), workspace_dir)
        registry = create_registry(config, memory, runtime_context=runtime_context)
        return NativeAgent(
            provider=provider,
            tool_registry=registry,
            memory=memory,
            system_prompt=cfg.system_prompt or "",
            default_model=cfg.model or config["provider"].get("model", ""),
            temperature=cfg.temperature or 0.7,
        )

    # Build runtime
    session_manager = SessionManager(workspace_dir)
    runtime = Runtime(session_manager)

    # Register agent factories
    runtime.register_agent("ccagent", build_ccagent)
    runtime.register_agent("native", build_native_agent)

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
