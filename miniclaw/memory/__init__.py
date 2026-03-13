"""Memory factory."""

from .base import Memory
from .json_memory import JsonMemory


def create_memory(config: dict) -> Memory:
    """Create a memory backend from config."""
    path = config.get("path", "memory.json")
    return JsonMemory(path=path)
