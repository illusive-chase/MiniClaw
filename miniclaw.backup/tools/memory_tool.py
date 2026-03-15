"""Memory tool — store, recall, and forget information."""

from miniclaw.memory.base import Memory

from .base import Tool, ToolResult


class MemoryTool(Tool):
    def __init__(self, memory: Memory):
        self._memory = memory

    def name(self) -> str:
        return "memory"

    def description(self) -> str:
        return (
            "Store, recall, or forget information in persistent memory. "
            "Use 'store' to save facts, 'recall' to search memories, "
            "'get' to retrieve by key, 'forget' to remove, 'list' to see all keys."
        )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "The memory action to perform",
                    "enum": ["store", "recall", "get", "forget", "list"],
                },
                "key": {
                    "type": "string",
                    "description": "Memory key (required for store, get, forget)",
                    "default": "",
                },
                "content": {
                    "type": "string",
                    "description": "Content to store (required for store)",
                    "default": "",
                },
                "category": {
                    "type": "string",
                    "description": "Category tag for the memory (optional, for store)",
                    "default": "",
                },
                "query": {
                    "type": "string",
                    "description": "Search query (required for recall)",
                    "default": "",
                },
            },
            "required": ["action"],
        }

    async def execute(self, args: dict) -> ToolResult:
        action = args.get("action", "")

        if action == "store":
            key = args.get("key", "")
            content = args.get("content", "")
            if not key or not content:
                return ToolResult(output="Both 'key' and 'content' are required for store", success=False)
            await self._memory.store(key, content, args.get("category", ""))
            return ToolResult(output=f"Stored memory: {key}")

        elif action == "recall":
            query = args.get("query", "")
            if not query:
                return ToolResult(output="'query' is required for recall", success=False)
            results = await self._memory.recall(query)
            if not results:
                return ToolResult(output="No memories found matching query")
            lines = []
            for r in results:
                lines.append(f"- [{r['key']}] {r['content']}")
            return ToolResult(output="\n".join(lines))

        elif action == "get":
            key = args.get("key", "")
            if not key:
                return ToolResult(output="'key' is required for get", success=False)
            entry = await self._memory.get(key)
            if entry:
                return ToolResult(output=f"[{entry['key']}] {entry['content']}")
            return ToolResult(output=f"No memory found for key: {key}")

        elif action == "forget":
            key = args.get("key", "")
            if not key:
                return ToolResult(output="'key' is required for forget", success=False)
            removed = await self._memory.forget(key)
            if removed:
                return ToolResult(output=f"Removed memory: {key}")
            return ToolResult(output=f"No memory found for key: {key}")

        elif action == "list":
            keys = await self._memory.list_keys()
            if not keys:
                return ToolResult(output="No memories stored")
            return ToolResult(output="Stored keys:\n" + "\n".join(f"- {k}" for k in keys))

        else:
            return ToolResult(output=f"Unknown action: {action}", success=False)
