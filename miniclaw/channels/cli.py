"""CLIChannel — output rendering for the terminal."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text
from rich.theme import Theme

from miniclaw.activity import (
    ActivityEvent,
    ActivitySnapshot,
    ActivityStatus,
    ActivityTracker,
)
from miniclaw.channels.base import Channel
from miniclaw.interactions import (
    InteractionRequest,
    InteractionResponse,
    InteractionType,
)
from miniclaw.providers.base import ChatMessage
from miniclaw.types import AgentEvent, InterruptedEvent, TextDelta, UsageEvent

logger = logging.getLogger(__name__)


def _format_elapsed(start: float, end: float | None = None) -> str:
    elapsed = (end if end is not None else time.monotonic()) - start
    if elapsed < 60:
        return f"{elapsed:.0f}s"
    minutes = int(elapsed) // 60
    seconds = int(elapsed) % 60
    return f"{minutes}m{seconds:02d}s"


class ActivityFooter:
    """Rich renderable showing real-time tool/subagent activity status."""

    def __init__(self) -> None:
        self._snapshot: ActivitySnapshot | None = None

    def update(self, snapshot: ActivitySnapshot) -> None:
        self._snapshot = snapshot

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        snap = self._snapshot
        if snap is None or not snap.has_activity:
            return

        if snap.tool_total > 0:
            line = Text()
            line.append("  Tools: ", style="bold dim")
            line.append(
                f"{snap.tool_done}/{snap.tool_total} done",
                style="bold yellow" if snap.tool_done < snap.tool_total else "dim",
            )
            if snap.tool_earliest:
                line.append(f"  [{_format_elapsed(snap.tool_earliest, snap.tool_finished)}]", style="bold cyan")
            yield line

            for recent in snap.tool_recents:
                detail = Text(no_wrap=True, overflow="ellipsis")
                if recent.status in (ActivityStatus.START, ActivityStatus.PROGRESS):
                    detail.append("    \u25cf ", style="bold yellow")
                elif recent.status == ActivityStatus.FINISH:
                    detail.append("    \u2713 ", style="green")
                elif recent.status == ActivityStatus.FAILED:
                    detail.append("    \u2717 ", style="bold red")
                if recent.summary:
                    detail.append(f'{recent.name}("{recent.summary}")', style="italic")
                else:
                    detail.append(recent.name, style="italic")
                if recent.status in (ActivityStatus.START, ActivityStatus.PROGRESS):
                    detail.append(f"  [{_format_elapsed(recent.timestamp)}]", style="bold cyan")
                elif recent.finished is not None:
                    detail.append(f"  [{_format_elapsed(recent.timestamp, recent.finished)}]", style="dim")
                yield detail

        if snap.agent_total > 0:
            line = Text()
            line.append("  Agents: ", style="bold dim")
            line.append(
                f"{snap.agent_done}/{snap.agent_total} done",
                style="bold yellow" if snap.agent_done < snap.agent_total else "dim",
            )
            if snap.agent_earliest:
                line.append(f"  [{_format_elapsed(snap.agent_earliest, snap.agent_finished)}]", style="bold cyan")
            yield line

            for recent in snap.agent_recents:
                detail = Text(no_wrap=True, overflow="ellipsis")
                if recent.status in (ActivityStatus.START, ActivityStatus.PROGRESS):
                    detail.append("    \u25cf ", style="bold yellow")
                elif recent.status == ActivityStatus.FINISH:
                    detail.append("    \u2713 ", style="green")
                elif recent.status == ActivityStatus.FAILED:
                    detail.append("    \u2717 ", style="bold red")
                if recent.summary:
                    detail.append(f'{recent.name}("{recent.summary}")', style="italic")
                else:
                    detail.append(recent.name, style="italic")
                if recent.status in (ActivityStatus.START, ActivityStatus.PROGRESS):
                    detail.append(f"  [{_format_elapsed(recent.timestamp)}]", style="bold cyan")
                elif recent.finished is not None:
                    detail.append(f"  [{_format_elapsed(recent.timestamp, recent.finished)}]", style="dim")
                yield detail


class StreamDisplay:
    """Composite renderable: Panel + ActivityFooter."""

    def __init__(self, panel: Panel, footer: ActivityFooter) -> None:
        self._panel = panel
        self._footer = footer

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield self._panel
        yield from self._footer.__rich_console__(console, options)


class CLIChannel(Channel):
    """Interactive command-line channel for terminal rendering."""

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console(theme=Theme({
            "markdown.code": "bold magenta on white",
            "markdown.code_block": "magenta on white",
            "markdown.hr": "gray70",
        }))

    async def send_stream(self, stream: AsyncIterator[AgentEvent]) -> None:
        """Stream response to the console with progressive markdown."""
        buffer = ""
        tracker = ActivityTracker()
        footer = ActivityFooter()
        live = Live(console=self._console, refresh_per_second=8)
        live.start()

        try:
            spinner = Spinner("dots", text="Thinking...", style="bold cyan")
            live.update(StreamDisplay(Panel(spinner, title="Assistant", border_style="blue"), footer))

            async for event in stream:
                if isinstance(event, ActivityEvent):
                    tracker.apply(event)
                    footer.update(tracker.snapshot())
                    panel = Panel(Markdown(buffer), title="Assistant", border_style="blue")
                    live.update(StreamDisplay(panel, footer))

                elif isinstance(event, InteractionRequest):
                    live.stop()
                    response = await self._prompt_interaction(event)
                    event.resolve(response)
                    live.start()

                elif isinstance(event, TextDelta):
                    buffer += event.text
                    panel = Panel(Markdown(buffer), title="Assistant", border_style="blue")
                    live.update(StreamDisplay(panel, footer))

                elif isinstance(event, UsageEvent):
                    u = event.usage
                    total = u.input_tokens + u.output_tokens
                    buffer += f"\n\n> tokens: {total:,} ({u.input_tokens:,} in + {u.output_tokens:,} out)"
                    panel = Panel(Markdown(buffer), title="Assistant", border_style="blue")
                    live.update(StreamDisplay(panel, footer))

                elif isinstance(event, InterruptedEvent):
                    buffer += "\n\n[interrupted]"
                    panel = Panel(Markdown(buffer), title="Assistant", border_style="yellow")
                    live.update(panel)

        finally:
            if buffer:
                live.update(Panel(Markdown(buffer), title="Assistant", border_style="blue"))
            live.stop()

    async def send(self, text: str) -> None:
        self._console.print(Panel(Markdown(text), title="Assistant", border_style="blue"))

    async def replay(self, history: list[ChatMessage]) -> None:
        for msg in history:
            if msg.role == "user" and msg.content:
                self._console.print(f"\n[bold green]You:[/] {msg.content}")
            elif msg.role == "assistant" and msg.content:
                self._console.print(Panel(Markdown(msg.content), title="Assistant", border_style="blue"))

    # --- Interaction prompts (reused from original CLIChannel) ---

    async def _prompt_interaction(self, request: InteractionRequest) -> InteractionResponse:
        if request.type == InteractionType.PERMISSION:
            return await self._prompt_permission(request)
        elif request.type == InteractionType.ASK_USER:
            return await self._prompt_ask_user(request)
        elif request.type == InteractionType.PLAN_APPROVAL:
            return await self._prompt_plan_approval(request)
        return InteractionResponse(id=request.id, allow=True)

    async def _prompt_permission(self, request: InteractionRequest) -> InteractionResponse:
        tool_input = request.tool_input
        summary_lines = [f"[bold]Tool:[/bold] {request.tool_name}"]

        if request.tool_name == "Bash" and "command" in tool_input:
            summary_lines.append(f"[bold]Command:[/bold] {tool_input['command']}")
        elif request.tool_name in ("Edit", "Write") and "file_path" in tool_input:
            summary_lines.append(f"[bold]File:[/bold] {tool_input['file_path']}")
        else:
            for k, v in list(tool_input.items())[:3]:
                val_str = str(v)[:120]
                summary_lines.append(f"[bold]{k}:[/bold] {val_str}")

        content = "\n".join(summary_lines) + "\n\n[dim][1] Allow  [2] Deny[/dim]"
        self._console.print(Panel(content, title="Permission Request", border_style="yellow"))

        choice = input("> ").strip()
        if choice == "2":
            reason = input("Reason (optional): ").strip()
            return InteractionResponse(id=request.id, allow=False, message=reason or "Denied by user")
        return InteractionResponse(id=request.id, allow=True)

    async def _prompt_ask_user(self, request: InteractionRequest) -> InteractionResponse:
        tool_input = request.tool_input
        questions = tool_input.get("questions", [])
        answers: dict[str, str] = {}

        for q in questions:
            question_text = q.get("question", "")
            options = q.get("options", [])

            lines = [f"[bold]{question_text}[/bold]", ""]
            for i, opt in enumerate(options, 1):
                label = opt.get("label", "")
                desc = opt.get("description", "")
                lines.append(f"  [dim][{i}][/dim] {label}" + (f" \u2014 {desc}" if desc else ""))
            lines.append(f"  [dim][{len(options) + 1}][/dim] Other (type your answer)")

            self._console.print(Panel("\n".join(lines), title="Agent Question", border_style="cyan"))
            choice = input("> ").strip()

            other_idx = str(len(options) + 1)
            if choice == other_idx:
                custom = input("Your answer: ").strip()
                answers[question_text] = custom
            else:
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(options):
                        answers[question_text] = options[idx].get("label", choice)
                    else:
                        answers[question_text] = choice
                except ValueError:
                    answers[question_text] = choice

        updated_input = dict(tool_input)
        updated_input["answers"] = answers
        return InteractionResponse(id=request.id, allow=True, updated_input=updated_input)

    async def _prompt_plan_approval(self, request: InteractionRequest) -> InteractionResponse:
        tool_input = request.tool_input
        plan_content = tool_input.get("plan", "") or tool_input.get("content", "")

        if plan_content:
            self._console.print(Panel(Markdown(plan_content), title="Plan Review", border_style="green"))
        else:
            self._console.print(Panel("[dim]The agent has prepared a plan.[/dim]", title="Plan Review", border_style="green"))

        self._console.print(
            "[dim][1] Yes, clear context and auto-accept edits\n"
            "[2] Yes, auto-accept edits\n"
            "[3] Yes, manually approve edits\n"
            "[4] No, keep planning[/dim]"
        )
        choice = input("> ").strip()

        if choice == "1":
            return InteractionResponse(
                id=request.id, allow=False, clear_context=True,
                permission_mode="acceptEdits",
                message=plan_content or "Execute the plan as discussed.",
            )
        elif choice == "2":
            return InteractionResponse(id=request.id, allow=True, permission_mode="acceptEdits")
        elif choice == "3":
            return InteractionResponse(id=request.id, allow=True, permission_mode="default")
        else:
            feedback = input("Feedback (optional): ").strip()
            return InteractionResponse(id=request.id, allow=False, message=feedback or "Plan rejected by user")
