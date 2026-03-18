"""CLIChannel — output rendering for the terminal."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

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

    def __init__(self, console: Console) -> None:
        self._console = console

    @staticmethod
    def _fmt_k(n: int) -> str:
        """Format token counts compactly: 1500 -> '1.5k', 12345 -> '12k', 200000 -> '200k'."""
        if n < 1000:
            return str(n)
        k = n / 1000
        if k == int(k):
            return f"{int(k)}k"
        return f"{k:.1f}k"

    @staticmethod
    def _render_usage(event: UsageEvent) -> Text | Table:
        """Build a Rich renderable for the usage footer."""
        u = event.usage
        total = u.input_tokens + u.output_tokens

        if (
            event.context_tokens is not None
            and event.context_window is not None
            and event.context_window > 0
        ):
            pct = min(100, event.context_tokens * 100 // event.context_window)
            grid = Table.grid(padding=(0, 1))
            grid.add_column()  # Total
            grid.add_column()  # Context label
            grid.add_column()  # Progress bar
            grid.add_column()  # (Xk/Yk)
            grid.add_row(
                Text(f"Total: {total:,}", style="bold"),
                Text(f"Context ({pct}%):", style="dim"),
                ProgressBar(
                    total=event.context_window,
                    completed=event.context_tokens,
                    width=20,
                ),
                Text(
                    f"({CLIChannel._fmt_k(event.context_tokens)}"
                    f"/{CLIChannel._fmt_k(event.context_window)})",
                    style="dim",
                ),
            )
            return grid
        else:
            return Text(
                f"tokens: {total:,} = {u.input_tokens:,} + {u.output_tokens:,}"
            )

    @staticmethod
    def _render_spinner_usage(event: UsageEvent) -> Text | Table:
        """Build a Rich renderable for the spinner text during thinking."""
        u = event.usage
        total = u.input_tokens + u.output_tokens

        if (
            event.context_tokens is not None
            and event.context_window is not None
            and event.context_window > 0
        ):
            pct = min(100, event.context_tokens * 100 // event.context_window)
            grid = Table.grid(padding=(0, 1))
            grid.add_column()  # Thinking + Total
            grid.add_column()  # Context label
            grid.add_column()  # Progress bar
            grid.add_column()  # (Xk/Yk)
            grid.add_row(
                Text(f"Thinking... (Total: {total:,})", style="bold cyan"),
                Text(f"Context ({pct}%):", style="dim"),
                ProgressBar(
                    total=event.context_window,
                    completed=event.context_tokens,
                    width=20,
                ),
                Text(
                    f"({CLIChannel._fmt_k(event.context_tokens)}"
                    f"/{CLIChannel._fmt_k(event.context_window)})",
                    style="dim",
                ),
            )
            return grid
        else:
            return Text(
                f"Thinking... (tokens: {total:,} = {u.input_tokens:,} + {u.output_tokens:,})"
            )

    async def send_stream(self, stream: AsyncIterator[AgentEvent]) -> None:
        """Stream response to the console with progressive markdown."""
        buffer = ""
        empty_line = Text("")
        final_panel = None  # set when final UsageEvent arrives; preserved in finally
        tracker = ActivityTracker()
        footer = ActivityFooter()
        live = Live(console=self._console, refresh_per_second=8)
        live.start()

        try:
            spinner = Spinner("bouncingBall", text="Thinking...", style="bold cyan")
            live.update(StreamDisplay(Panel(spinner, title="Assistant", border_style="blue"), footer))

            async for event in stream:
                if isinstance(event, ActivityEvent):
                    tracker.apply(event)
                    footer.update(tracker.snapshot())
                    content = Group(Markdown(buffer), empty_line, spinner) if buffer else spinner
                    panel = Panel(content, title="Assistant", border_style="blue")
                    live.update(StreamDisplay(panel, footer))

                elif isinstance(event, InteractionRequest):
                    live.stop()
                    response = await self._prompt_interaction(event)
                    event.resolve(response)
                    live.start()

                elif isinstance(event, TextDelta):
                    buffer += event.text
                    panel = Panel(Group(Markdown(buffer), empty_line, spinner), title="Assistant", border_style="blue")
                    live.update(StreamDisplay(panel, footer))

                elif isinstance(event, UsageEvent):
                    if event.final:
                        usage_renderable = self._render_usage(event)
                        content = Group(Markdown(buffer), Text(""), usage_renderable) if buffer else usage_renderable
                        final_panel = Panel(content, title="Assistant", border_style="blue")
                        live.update(StreamDisplay(final_panel, footer))
                    else:
                        # Intermediate: update spinner text with running token count
                        spinner.text = self._render_spinner_usage(event)
                        content = Group(Markdown(buffer), empty_line, spinner) if buffer else spinner
                        panel = Panel(content, title="Assistant", border_style="blue")
                        live.update(StreamDisplay(panel, footer))

                elif isinstance(event, InterruptedEvent):
                    buffer += "\n\n[interrupted]"
                    final_panel = Panel(Markdown(buffer), title="Assistant", border_style="yellow")
                    live.update(final_panel)

        finally:
            if final_panel:
                live.update(final_panel)
            elif buffer:
                live.update(Panel(Markdown(buffer), title="Assistant", border_style="blue"))
            live.stop()

    async def send(self, text: str) -> None:
        self._console.print(Panel(Markdown(text), title="Assistant", border_style="blue"))

    async def on_observe(self, stream: AsyncIterator[AgentEvent]) -> None:
        """Render events as a read-only observer without a permanent Live display.

        Unlike send_stream(), this does NOT show a spinner when idle.
        A Live display is only created when events actually arrive (start of
        a turn) and stopped when the turn completes (UsageEvent / InterruptedEvent).
        This prevents the "infinite Thinking..." problem and avoids interfering
        with the REPL's prompt_toolkit input.
        """
        buffer = ""
        tracker = ActivityTracker()
        footer = ActivityFooter()
        live: Live | None = None

        async def _auto_resolve(
            source: AsyncIterator[AgentEvent],
        ) -> AsyncIterator[AgentEvent]:
            async for event in source:
                if isinstance(event, InteractionRequest):
                    event.resolve(InteractionResponse(id=event.id, allow=True))
                else:
                    yield event

        try:
            async for event in _auto_resolve(stream):
                # Start Live on first event of a new turn
                if live is None:
                    buffer = ""
                    empty_line = Text("")
                    tracker = ActivityTracker()
                    footer = ActivityFooter()
                    spinner = Spinner("bouncingBall", text="Thinking...", style="bold cyan")
                    live = Live(console=self._console, refresh_per_second=8)
                    live.start()

                if isinstance(event, ActivityEvent):
                    tracker.apply(event)
                    footer.update(tracker.snapshot())
                    content = Group(Markdown(buffer), empty_line, spinner) if buffer else spinner
                    panel = Panel(content, title="Assistant", border_style="blue")
                    live.update(StreamDisplay(panel, footer))

                elif isinstance(event, TextDelta):
                    buffer += event.text
                    panel = Panel(Group(Markdown(buffer), empty_line, spinner), title="Assistant", border_style="blue")
                    live.update(StreamDisplay(panel, footer))

                elif isinstance(event, UsageEvent):
                    if event.final:
                        usage_renderable = self._render_usage(event)
                        content = Group(Markdown(buffer), Text(""), usage_renderable) if buffer else usage_renderable
                        # Turn complete — finalize and stop Live
                        if live is not None:
                            live.update(Panel(content, title="Assistant", border_style="blue"))
                            live.stop()
                            live = None
                    else:
                        # Intermediate: update spinner text with running token count
                        spinner.text = self._render_spinner_usage(event)
                        content = Group(Markdown(buffer), empty_line, spinner) if buffer else spinner
                        panel = Panel(content, title="Assistant", border_style="blue")
                        if live is not None:
                            live.update(StreamDisplay(panel, footer))

                elif isinstance(event, InterruptedEvent):
                    buffer += "\n\n[interrupted]"
                    if live is not None:
                        live.update(Panel(Markdown(buffer), title="Assistant", border_style="yellow"))
                        live.stop()
                        live = None

        finally:
            # Cleanup on detach (task cancellation) or stream end
            if live is not None:
                if buffer:
                    live.update(Panel(Markdown(buffer), title="Assistant", border_style="blue"))
                live.stop()

    async def replay(self, history: list[ChatMessage]) -> None:
        for msg in history:
            if msg.role == "user" and msg.content:
                self._console.print(Panel(msg.content, title="You", border_style="green"))
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
