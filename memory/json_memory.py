"""JSON file memory backend."""

import json
from datetime import datetime, timezone
from pathlib import Path

from memory.base import Memory


class JsonMemory(Memory):
    """Simple JSON file-backed memory store."""

    def __init__(self, path: str = "memory.json"):
        self._path = Path(path)
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self):
        if self._path.exists():
            with open(self._path) as f:
                self._data = json.load(f)

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    async def store(self, key: str, content: str, category: str = "") -> None:
        self._data[key] = {
            "content": content,
            "category": category,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    async def recall(self, query: str, limit: int = 5) -> list[dict]:
        query_lower = query.lower()
        scored = []
        for key, entry in self._data.items():
            text = f"{key} {entry.get('content', '')} {entry.get('category', '')}"
            if query_lower in text.lower():
                scored.append({"key": key, **entry})
        scored.sort(key=lambda e: e.get("updated_at", ""), reverse=True)
        return scored[:limit]

    async def get(self, key: str) -> dict | None:
        entry = self._data.get(key)
        if entry:
            return {"key": key, **entry}
        return None

    async def forget(self, key: str) -> bool:
        if key in self._data:
            del self._data[key]
            self._save()
            return True
        return False

    async def list_keys(self) -> list[str]:
        return list(self._data.keys())
