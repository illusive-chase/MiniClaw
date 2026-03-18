"""Feishu open doc reader tool — extracts markdown via the built-in '复制页面' button."""

import asyncio
import json
import os
from pathlib import Path

from .base import Tool, ToolResult


async def _run(cmd: str, timeout: int = 30) -> tuple[str, bool]:
    """Run a shell command, return (output, success)."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    out = stdout.decode(errors="replace")
    if stderr:
        out += "\n" + stderr.decode(errors="replace")
    return out.strip(), proc.returncode == 0


class FeishuDocTool(Tool):
    def __init__(self, cwd: str = "."):
        self._cwd = Path(cwd)

    def name(self) -> str:
        return "feishu_doc_read"

    def description(self) -> str:
        return (
            "Read a Feishu open doc page and return its content as markdown. "
            "Uses the built-in '复制页面' (Copy Page) button to extract clean markdown with "
            "headings, links, tables, bold text, and image references. "
            "This tool cannot be used parallelly. Make sure only one instance is running at a time."
        )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Feishu open doc URL (e.g. https://open.feishu.cn/document/...)",
                },
                "save_path": {
                    "type": "string",
                    "description": "Optional file path to save the markdown output to.",
                },
            },
            "required": ["url"],
        }

    async def execute(self, args: dict) -> ToolResult:
        url = args.get("url", "").strip()
        save_path = args.get("save_path", "").strip() or None
        if not url:
            return ToolResult(output="No URL provided", success=False)

        timeout = 60
        session = "feishu_doc"
        try:
            # 1. Open the page
            out, ok = await _run(
                f"playwright-cli -s={session} open '{url}'", timeout=timeout
            )
            if not ok:
                return ToolResult(output=f"Failed to open page:\n{out}", success=False)

            # 2. Wait for page to settle, then click the "复制页面" button
            click_script = (
                "async page => {"
                "  await page.waitForTimeout(3000);"
                "  const btn = page.getByRole('button', { name: '复制页面' });"
                "  await btn.waitFor({ state: 'visible', timeout: 15000 });"
                "  await btn.click();"
                "}"
            )
            out, ok = await _run(
                f"playwright-cli -s={session} run-code \"{click_script}\"",
                timeout=timeout,
            )
            if not ok:
                return ToolResult(
                    output=f"Failed to click '复制页面' button:\n{out}", success=False
                )

            # 3. Wait a moment for clipboard to populate, then read clipboard
            await asyncio.sleep(1)
            read_script = (
                "async page => {"
                "  await page.context().grantPermissions(['clipboard-read']);"
                "  const text = await page.evaluate(() => navigator.clipboard.readText());"
                "  return text;"
                "}"
            )
            out, ok = await _run(
                f"playwright-cli -s={session} run-code \"{read_script}\"",
                timeout=timeout,
            )
            if not ok:
                return ToolResult(
                    output=f"Failed to read clipboard:\n{out}", success=False
                )

            # 4. Parse markdown from playwright-cli output
            # The output contains a "### Result" section with the returned value
            markdown = _parse_run_code_result(out)
            if not markdown:
                return ToolResult(
                    output=f"Clipboard was empty or could not parse result:\n{out}",
                    success=False,
                )

            # 5. Optionally save to file
            if save_path:
                p = Path(save_path)
                if not p.is_absolute():
                    p = self._cwd / p
                os.makedirs(p.parent, exist_ok=True)
                with open(p, "w", encoding="utf-8") as f:
                    f.write(markdown)

            return ToolResult(output=markdown, success=True)

        except asyncio.TimeoutError:
            return ToolResult(
                output=f"Operation timed out after {timeout} seconds", success=False
            )
        except Exception as e:
            return ToolResult(output=f"Error: {e}", success=False)
        finally:
            # Always close the browser session
            await _run(f"playwright-cli -s={session} close", timeout=10)


def _parse_run_code_result(output: str) -> str | None:
    """Extract the returned string from playwright-cli run-code output.

    The output looks like:
        ### Result
        "some text here with \\n escapes"

    or for multiline results it may be a JSON-encoded string.
    """
    marker = "### Result"
    idx = output.find(marker)
    if idx == -1:
        return None
    result_section = output[idx + len(marker) :].strip()
    if not result_section:
        return None
    # The result is a JSON-encoded string (double-quoted with escapes)
    try:
        return json.loads(result_section.split("\n###")[0].strip())
    except (json.JSONDecodeError, ValueError):
        # Fallback: return the raw text (strip surrounding quotes if present)
        text = result_section.split("\n###")[0].strip()
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        return text or None
