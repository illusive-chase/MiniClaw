"""Channel factory."""

from .base import Channel
from .cli import CLIChannel


def create_channel(config: dict) -> Channel:
    """Create a channel from config."""
    channel_type = config.get("type", "cli")

    if channel_type == "feishu":
        from miniclaw.channels.feishu import FeishuChannel
        return FeishuChannel(
            app_id=config.get("app_id", ""),
            app_secret=config.get("app_secret", ""),
            verification_token=config.get("verification_token", ""),
        )
    else:
        return CLIChannel(config)
