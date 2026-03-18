"""Config loader with environment variable interpolation."""

import os
import re
from pathlib import Path

import yaml


def get_codebase_root() -> Path:
    """Return the codebase root (parent of the miniclaw/ package directory)."""
    return Path(__file__).resolve().parent.parent


CODEBASE_ROOT = get_codebase_root()


def _interpolate_env(value: str) -> str:
    """Replace ${VAR} patterns with environment variable values."""

    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, "")

    return re.sub(r"\$\{(\w+)\}", replacer, value)


def _interpolate_recursive(obj):
    """Recursively interpolate environment variables in config values."""
    if isinstance(obj, str):
        return _interpolate_env(obj)
    if isinstance(obj, dict):
        return {k: _interpolate_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate_recursive(item) for item in obj]
    return obj


DEFAULT_CONFIG = {
    "provider": {
        "type": "openai",
        "api_key": "",
        "base_url": None,
        "model": "gpt-4o",
        "temperature": 0.7,
        "max_tokens": 8192,
        "context_window": 0,
    },
    "channel": {
        "type": "cli",
        "app_id": "",
        "app_secret": "",
    },
    "agent": {
        "system_prompt": (
            "You are a helpful assistant with access to tools. "
            "Use tools when needed to answer questions and complete tasks. "
        ),
        "max_tool_iterations": 50,
        "workspace_dir": ".workspace",
        "tool_deny_list": ['shell'],
    },
    "logging": {
        "file_level": "debug",
        "console_level": "info",
    },
    "ccagent": {
        "system_prompt": "",
        "model": "claude-opus-4-6",
        "permission_mode": "default",
        "allowed_tools": [
            "Read",
            "Write",
            "Edit",
            "Bash",
            "Glob",
            "Grep",
            "WebSearch",
            "WebFetch",
            "AskUserQuestion",
            "Agent",
            "EnterPlanMode",
            "ExitPlanMode",
        ],
        "max_turns": None,
        "cwd": None,
        "thinking": {"type": "adaptive"},
        "effort": "high",
    },
    "plugctx": {
        "ctx_root": ".workspace/contexts",
        "auto_load": [],
    },
    "remotes": {},
}


def _resolve_config_paths(config: dict) -> dict:
    """Resolve relative infrastructure paths against CODEBASE_ROOT."""
    workspace_dir = config.get("agent", {}).get("workspace_dir", "")
    if workspace_dir and not Path(workspace_dir).is_absolute():
        config["agent"]["workspace_dir"] = str(CODEBASE_ROOT / workspace_dir)

    ctx_root = config.get("plugctx", {}).get("ctx_root", "")
    if ctx_root and not Path(ctx_root).is_absolute():
        config["plugctx"]["ctx_root"] = str(CODEBASE_ROOT / ctx_root)

    return config


def load_config(path: str = "config.yaml") -> dict:
    """Load config from YAML file, merge with defaults, interpolate env vars."""
    config = dict(DEFAULT_CONFIG)
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = CODEBASE_ROOT / config_path
    if config_path.exists():
        with open(config_path) as f:
            user_config = yaml.safe_load(f) or {}
        for section, values in user_config.items():
            if isinstance(values, dict) and section in config:
                config[section] = {**config[section], **values}
            else:
                config[section] = values
    config = _interpolate_recursive(config)
    config = _resolve_config_paths(config)
    return config
