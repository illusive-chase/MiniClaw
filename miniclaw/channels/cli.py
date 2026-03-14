"""CLI channel for local testing via stdin/stdout."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.logging import RichHandler
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
from miniclaw.interactions import (
    InteractionRequest,
    InteractionResponse,
    InteractionType,
)

from .base import Channel, SendMessage
from .commands import CommandContext, create_default_registry

if TYPE_CHECKING:
    from miniclaw.gateway import Gateway

logger = logging.getLogger(__name__)


def _format_elapsed(start: float, end: float | None = None) -> str:
    """Format elapsed time from a monotonic timestamp.

    If *end* is provided the duration is frozen; otherwise it ticks live.
    """
    elapsed = (end if end is not None else time.monotonic()) - start
    if elapsed < 60:
        return f"{elapsed:.0f}s"
    minutes = int(elapsed) // 60
    seconds = int(elapsed) % 60
    return f"{minutes}m{seconds:02d}s"


class ActivityFooter:
    """Rich renderable that shows real-time tool/subagent activity status."""

    def __init__(self) -> None:
        self._snapshot: ActivitySnapshot | None = None

    def update(self, snapshot: ActivitySnapshot) -> None:
        self._snapshot = snapshot

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        snap = self._snapshot
        if snap is None or not snap.has_activity:
            return


        # Tools line
        if snap.tool_total > 0:
            line = Text()
            line.append("  Tools: ", style="bold dim")
            line.append(f"{snap.tool_done}/{snap.tool_total} done", style="bold yellow" if snap.tool_done < snap.tool_total else "dim")
            if snap.tool_earliest:
                line.append(f"  [{_format_elapsed(snap.tool_earliest, snap.tool_finished)}]", style="bold cyan")
            yield line

            for recent in snap.tool_recents:
                detail = Text()
                if recent.status in (ActivityStatus.START, ActivityStatus.PROGRESS):
                    detail.append("    ● ", style="bold yellow")
                elif recent.status == ActivityStatus.FINISH:
                    detail.append("    ✓ ", style="green")
                elif recent.status == ActivityStatus.FAILED:
                    detail.append("    ✗ ", style="bold red")

                summary = recent.summary
                if summary:
                    label = summary if len(summary) <= 50 else summary[:47] + "..."
                    detail.append(f"{recent.name}(\"{label}\")", style="italic")
                else:
                    detail.append(recent.name, style="italic")
                if recent.status in (ActivityStatus.START, ActivityStatus.PROGRESS):
                    detail.append(f"  [{_format_elapsed(recent.timestamp)}]", style="bold cyan")
                elif recent.finished is not None:
                    detail.append(f"  [{_format_elapsed(recent.timestamp, recent.finished)}]", style="dim")
                yield detail

        # Agents section
        if snap.agent_total > 0:
            line = Text()
            line.append("  Agents: ", style="bold dim")
            line.append(f"{snap.agent_done}/{snap.agent_total} done", style="bold yellow" if snap.agent_done < snap.agent_total else "dim")
            if snap.agent_earliest:
                line.append(f"  [{_format_elapsed(snap.agent_earliest, snap.agent_finished)}]", style="bold cyan")
            yield line

            for recent in snap.agent_recents:
                detail = Text()
                if recent.status in (ActivityStatus.START, ActivityStatus.PROGRESS):
                    detail.append("    ● ", style="bold yellow")
                elif recent.status == ActivityStatus.FINISH:
                    detail.append("    ✓ ", style="green")
                elif recent.status == ActivityStatus.FAILED:
                    detail.append("    ✗ ", style="bold red")

                summary = recent.summary
                if summary:
                    label = summary if len(summary) <= 50 else summary[:47] + "..."
                    detail.append(f"{recent.name}(\"{label}\")", style="italic")
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
    """Interactive command-line channel for testing."""

    def __init__(self, config: dict | None = None):
        config = config or {}
        self._registry = create_default_registry()
        self._gw: Gateway | None = None
        self._session_id: str | None = None

        self._console = Console(theme=Theme({
            "markdown.code": "bold magenta on white",
            "markdown.code_block": "magenta on white",
            "markdown.hr": "gray70",
        }))

        console_level = config.get("console_level", logging.INFO)
        self._console_handler = RichHandler(
            console=self._console,
            rich_tracebacks=True,
            tracebacks_show_locals=False,
        )
        self._console_handler.setFormatter(
            logging.Formatter("%(message)s", datefmt="[%X]")
        )
        self._console_handler.setLevel(console_level)

    def log_handler(self) -> logging.Handler | None:
        return self._console_handler

    def replay_message(self, role: str, text: str) -> None:
        """Replay a historical message during session resume."""
        if role == "user":
            self._console.print(f"\n[bold green]You:[/] {text}")
        elif role == "assistant":
            self._console.print(Panel(Markdown(text), title="Assistant", border_style="blue"))

    def command_descriptions(self) -> list[dict]:
        """Return all command descriptions for /help."""
        return [
            {"name": cmd.name(), "description": cmd.description(), "usage": cmd.usage()}
            for cmd in self._registry.all_commands()
        ]

    async def start(self, gateway: Gateway) -> None:
        """Called by Gateway. Allocate session and begin listen loop."""
        self._gw = gateway
        self._session_id = gateway.allocate_session("cli_user")

        self._console.print(Panel("MiniClaw CLI", subtitle="type /help for commands", style="bold cyan"))
        loop = asyncio.get_event_loop()
        while True:
            try:
                self._console.print("\n[bold green]You:[/] ", end="")
                line = await loop.run_in_executor(None, sys.stdin.readline)
                line = line.strip()
                if not line:
                    continue

                # Backward compat: bare quit/exit
                if line.lower() in ("quit", "exit"):
                    self._console.print("[dim]Goodbye![/dim]")
                    break

                # Slash commands
                if line.startswith("/"):
                    ctx = CommandContext(
                        channel=self,
                        gateway=self._gw,
                        session_id=self._session_id,
                    )
                    resolved = self._registry.resolve(line[1:])
                    if resolved:
                        cmd, args = resolved
                        try:
                            result = await cmd.execute(args, ctx)
                            # Update session_id in case a command changed it (e.g. /resume)
                            self._session_id = ctx.channel._session_id
                            if result:
                                self._console.print(result)
                        except SystemExit:
                            self._console.print("[dim]Goodbye![/dim]")
                            break
                    else:
                        self._console.print(f"Unknown command: {line}. Type /help for available commands.")
                    continue

                # Regular message — use streaming if gateway supports it
                if hasattr(self._gw, "process_message_stream"):
                    stream = self._gw.process_message_stream(self._session_id, line)
                    await self.send_stream(stream)
                else:
                    with self._console.status("[bold cyan]Thinking...", spinner="dots"):
                        reply = await self._gw.process_message(self._session_id, line)
                    await self.send(SendMessage(text=reply))
            except (EOFError, KeyboardInterrupt):
                self._console.print("\n[dim]Goodbye![/dim]")
                break

    async def send(self, message: SendMessage) -> None:
        self._console.print(Panel(Markdown(message.text), title="Assistant", border_style="blue"))

    async def send_stream(self, stream: AsyncIterator[str | InteractionRequest | ActivityEvent]) -> None:
        """Stream response chunks to the console with progressive markdown.

        Handles InteractionRequests inline: pauses rendering, prompts the user,
        resolves the interaction, then resumes. ActivityEvents update a status
        footer below the response panel (markdown mode only).
        """

        # Progressive markdown rendering with activity footer
        buffer = ""
        tracker = ActivityTracker()
        footer = ActivityFooter()
        live = Live(console=self._console, refresh_per_second=8)
        live.start()
        try:
            # Show thinking spinner until first content arrives
            spinner = Spinner("dots", text="Thinking...", style="bold cyan")
            live.update(StreamDisplay(Panel(spinner, title="Assistant", border_style="blue"), footer))

            async for chunk in stream:
                if isinstance(chunk, ActivityEvent):
                    tracker.apply(chunk)
                    footer.update(tracker.snapshot())
                    panel = Panel(Markdown(buffer), title="Assistant", border_style="blue")
                    live.update(StreamDisplay(panel, footer))
                elif isinstance(chunk, InteractionRequest):
                    # Pause live rendering — SDK is blocked, no new chunks coming
                    live.stop()
                    response = await self._prompt_interaction(chunk)
                    chunk.resolve(response)
                    # Resume live rendering (footer state preserved)
                    live.start()
                else:
                    buffer += chunk
                    panel = Panel(Markdown(buffer), title="Assistant", border_style="blue")
                    live.update(StreamDisplay(panel, footer))
        finally:
            live.stop()

    # --- Interaction prompts ---

    async def _prompt_interaction(self, request: InteractionRequest) -> InteractionResponse:
        """Prompt the user for an interactive decision."""
        loop = asyncio.get_event_loop()

        if request.type == InteractionType.PERMISSION:
            return await self._prompt_permission(request, loop)
        elif request.type == InteractionType.ASK_USER:
            return await self._prompt_ask_user(request, loop)
        elif request.type == InteractionType.PLAN_APPROVAL:
            return await self._prompt_plan_approval(request, loop)
        else:
            # Unknown type — auto-allow
            return InteractionResponse(id=request.id, allow=True)

    async def _prompt_permission(
        self, request: InteractionRequest, loop: asyncio.AbstractEventLoop
    ) -> InteractionResponse:
        """Display a tool permission request and prompt for allow/deny."""
        tool_input = request.tool_input

        # Build a readable summary of the tool input
        summary_lines = [f"[bold]Tool:[/bold] {request.tool_name}"]
        if request.tool_name == "Bash" and "command" in tool_input:
            summary_lines.append(f"[bold]Command:[/bold] {tool_input['command']}")
        elif request.tool_name == "Edit" and "file_path" in tool_input:
            summary_lines.append(f"[bold]File:[/bold] {tool_input['file_path']}")
            if "old_string" in tool_input:
                old = tool_input["old_string"]
                preview = old[:120] + "..." if len(old) > 120 else old
                summary_lines.append(f"[bold]Replace:[/bold] {preview}")
        elif request.tool_name == "Write" and "file_path" in tool_input:
            summary_lines.append(f"[bold]File:[/bold] {tool_input['file_path']}")
        else:
            # Generic: show first few keys
            for k, v in list(tool_input.items())[:3]:
                val_str = str(v)
                if len(val_str) > 120:
                    val_str = val_str[:120] + "..."
                summary_lines.append(f"[bold]{k}:[/bold] {val_str}")

        content = "\n".join(summary_lines)
        content += "\n\n[dim][1] Allow  [2] Deny[/dim]"

        self._console.print(Panel(content, title="Permission Request", border_style="yellow"))

        choice = await loop.run_in_executor(None, lambda: input("> ").strip())

        if choice == "2":
            reason = await loop.run_in_executor(None, lambda: input("Reason (optional): ").strip())
            return InteractionResponse(id=request.id, allow=False, message=reason or "Denied by user")

        return InteractionResponse(id=request.id, allow=True)

    async def _prompt_ask_user(
        self, request: InteractionRequest, loop: asyncio.AbstractEventLoop
    ) -> InteractionResponse:
        """Display an AskUserQuestion interaction and collect the user's answer."""
        tool_input = request.tool_input
        questions = tool_input.get("questions", [])

        answers: dict[str, str] = {}
        for q in questions:
            question_text = q.get("question", "")
            options = q.get("options", [])
            multi = q.get("multiSelect", False)

            lines = [f"[bold]{question_text}[/bold]", ""]
            for i, opt in enumerate(options, 1):
                label = opt.get("label", "")
                desc = opt.get("description", "")
                if desc:
                    lines.append(f"  [dim][{i}][/dim] {label} — {desc}")
                else:
                    lines.append(f"  [dim][{i}][/dim] {label}")
            lines.append(f"  [dim][{len(options) + 1}][/dim] Other (type your answer)")

            if multi:
                lines.append("\n[dim]Enter numbers separated by commas[/dim]")

            self._console.print(Panel("\n".join(lines), title="Agent Question", border_style="cyan"))
            choice = await loop.run_in_executor(None, lambda: input("> ").strip())

            # Parse the choice
            other_idx = str(len(options) + 1)
            if multi:
                selected = [c.strip() for c in choice.split(",")]
                if other_idx in selected:
                    custom = await loop.run_in_executor(None, lambda: input("Your answer: ").strip())
                    answers[question_text] = custom
                else:
                    labels = []
                    for s in selected:
                        try:
                            idx = int(s) - 1
                            if 0 <= idx < len(options):
                                labels.append(options[idx].get("label", s))
                        except ValueError:
                            labels.append(s)
                    answers[question_text] = ", ".join(labels)
            else:
                if choice == other_idx:
                    custom = await loop.run_in_executor(None, lambda: input("Your answer: ").strip())
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

        # Build updated tool input with user's answers
        updated_input = dict(tool_input)
        updated_input["answers"] = answers

        return InteractionResponse(
            id=request.id,
            allow=True,
            updated_input=updated_input,
        )

    async def _prompt_plan_approval(
        self, request: InteractionRequest, loop: asyncio.AbstractEventLoop
    ) -> InteractionResponse:
        """Display a plan for review and prompt with 4 approval options."""
        tool_input = request.tool_input

        # Render plan content if available
        plan_content = tool_input.get("plan", "") or tool_input.get("content", "")
        if plan_content:
            self._console.print(Panel(
                Markdown(plan_content),
                title="Plan Review",
                border_style="green",
            ))
        else:
            self._console.print(Panel(
                "[dim]The agent has prepared a plan for your review.[/dim]",
                title="Plan Review",
                border_style="green",
            ))

        self._console.print(
            "[dim][1] Yes, clear context and auto-accept edits\n"
            "[2] Yes, auto-accept edits\n"
            "[3] Yes, manually approve edits\n"
            "[4] No, keep planning[/dim]"
        )
        choice = await loop.run_in_executor(None, lambda: input("> ").strip())

        if choice == "1":
            # Clear context + acceptEdits: pass plan content in message for PlanExecuteAction
            return InteractionResponse(
                id=request.id,
                allow=False,
                clear_context=True,
                permission_mode="acceptEdits",
                message=plan_content or "Execute the plan as discussed.",
            )
        elif choice == "2":
            # Approve + acceptEdits mode
            return InteractionResponse(
                id=request.id,
                allow=True,
                permission_mode="acceptEdits",
            )
        elif choice == "3":
            # Approve + default mode (manual approval for each edit)
            return InteractionResponse(
                id=request.id,
                allow=True,
                permission_mode="default",
            )
        else:
            # Keep planning — prompt for feedback
            feedback = await loop.run_in_executor(
                None, lambda: input("Feedback (optional): ").strip()
            )
            return InteractionResponse(
                id=request.id,
                allow=False,
                message=feedback or "Plan rejected by user",
            )
