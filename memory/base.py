"""Memory ABC for persistent storage."""

from abc import ABC, abstractmethod


class Memory(ABC):
    """Abstract base class for memory backends."""

    @abstractmethod
    async def store(self, key: str, content: str, category: str = "") -> None:
        """Store a memory entry."""
        ...

    @abstractmethod
    async def recall(self, query: str, limit: int = 5) -> list[dict]:
        """Recall memories matching a query."""
        ...

    @abstractmethod
    async def get(self, key: str) -> dict | None:
        """Get a specific memory entry by key."""
        ...

    @abstractmethod
    async def forget(self, key: str) -> bool:
        """Remove a memory entry. Returns True if it existed."""
        ...

    @abstractmethod
    async def list_keys(self) -> list[str]:
        """List all stored memory keys."""
        ...
