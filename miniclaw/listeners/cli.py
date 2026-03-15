"""CLIListener — interactive REPL loop for the terminal."""

from __future__ import annotations

import logging
import signal
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.panel import Panel

from miniclaw.agent.config import AgentConfig
from miniclaw.channels.cli import CLIChannel
from miniclaw.listeners.base import Listener

if TYPE_CHECKING:
    from miniclaw.runtime import Runtime
    from miniclaw.session import Session

logger = logging.getLogger(__name__)


class CLIListener(Listener):
    """Interactive REPL loop that drives a CLI session.

    Handles:
      - User input via prompt_toolkit
      - Slash commands (/reset, /sessions, /fork, /attach, /pipe, etc.)
      - SIGINT -> session.interrupt()
      - Message routing to session.process()
      - Stream rendering via CLIChannel
    """

    def __init__(
        self,
        agent_type: str = "native",
        agent_config: AgentConfig | None = None,
        workspace_dir: str = ".workspace",
        console: Console | None = None,
    ) -> None:
        self._agent_type = agent_type
        self._agent_config = agent_config or AgentConfig()
        self._workspace_dir = workspace_dir
        self._console = console
        self._session: Session | None = None

    async def run(self, runtime: Runtime) -> None:
        """Main REPL loop."""
        channel = CLIChannel(self._console)
        console = channel._console

        # Create session
        session = runtime.create_session(self._agent_type, self._agent_config)
        session.bind_primary(channel)
        self._session = session

        # Setup prompt
        history_path = Path(self._workspace_dir) / ".cli_history"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_session: PromptSession = PromptSession(
            history=FileHistory(str(history_path)),
        )

        # Install SIGINT handler
        original_handler = signal.getsignal(signal.SIGINT)

        def _sigint_handler(signum, frame):
            if self._session is not None:
                self._session.interrupt()

        signal.signal(signal.SIGINT, _sigint_handler)

        console.print(Panel("MiniClaw", subtitle="type /help for commands", style="bold cyan"))

        try:
            while True:
                try:
                    console.print()
                    line = await prompt_session.prompt_async(
                        HTML("<b><ansigreen>You:</ansigreen></b> ")
                    )
                    line = line.strip()
                    if not line:
                        continue

                    if line.lower() in ("quit", "exit"):
                        console.print("[dim]Goodbye![/dim]")
                        break

                    # Slash commands
                    if line.startswith("/"):
                        await self._handle_command(
                            line[1:], runtime, session, channel, console
                        )
                        continue

                    # Regular message
                    stream = session.process(line)
                    await channel.send_stream(stream)

                except (EOFError, KeyboardInterrupt):
                    console.print("\n[dim]Goodbye![/dim]")
                    break
        finally:
            signal.signal(signal.SIGINT, original_handler)

    async def _handle_command(
        self,
        command: str,
        runtime: Runtime,
        session: Session,
        channel: CLIChannel,
        console: Console,
    ) -> None:
        """Handle a slash command."""
        parts = command.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if cmd == "help":
            console.print(Panel(
                "[bold]Commands:[/bold]\n"
                "  /help             Show this help\n"
                "  /reset            Clear conversation history\n"
                "  /sessions         List saved sessions\n"
                "  /resume <id>      Resume a saved session\n"
                "  /fork <id>        Fork an existing session\n"
                "  /attach <id>      Attach as observer (read-only)\n"
                "  /detach           Detach from observed session\n"
                "  /pipe <id>        Connect current session to another via pipe\n"
                "  /unpipe <id>      Disconnect pipe to another session\n"
                "  /model [name]     Show or change model\n"
                "  /effort [level]   Show or set thinking effort (low/medium/high)\n"
                "  /cost             Show usage stats\n"
                "  /rename <name>    Rename current session\n"
                "  /logging <level>  Set console log level\n"
                "  /quit, /exit, /q  Exit the REPL",
                title="Help",
                border_style="cyan",
            ))

        elif cmd == "reset":
            count = session.clear_history()
            console.print(f"[dim]Cleared {count} messages.[/dim]")

        elif cmd == "sessions":
            sessions = runtime.list_persisted_sessions()
            if not sessions:
                console.print("[dim]No saved sessions.[/dim]")
            else:
                lines = []
                for s in sessions[:20]:
                    name = s.name or "unnamed"
                    lines.append(f"  {s.id}  {name}  ({s.updated_at})")
                console.print(Panel("\n".join(lines), title="Sessions", border_style="cyan"))

        elif cmd == "resume":
            if not args:
                console.print("[red]Usage: /resume <session_id>[/red]")
                return
            try:
                new_session = await runtime.restore_session(args)
                new_session.bind_primary(channel)
                self._session = new_session
                # Replace session reference for the REPL loop
                # Note: the REPL loop still holds the old `session` variable.
                # We update self._session for SIGINT, but commands go through
                # the local var. This is a known limitation — a proper fix
                # would use a session holder pattern.
                await channel.replay(new_session.history)
                console.print(f"[dim]Resumed session {new_session.id}[/dim]")
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")

        elif cmd == "fork":
            if not args:
                console.print("[red]Usage: /fork <session_id>[/red]")
                return
            try:
                forked = await runtime.fork_session(args)
                forked.bind_primary(channel)
                self._session = forked
                await channel.replay(forked.history)
                console.print(f"[dim]Forked to session {forked.id}[/dim]")
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")

        elif cmd == "attach":
            if not args:
                console.print("[red]Usage: /attach <session_id>[/red]")
                return
            try:
                runtime.attach_observer(args, channel)
                console.print(f"[dim]Attached as observer to {args}. Use /detach to leave.[/dim]")
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")

        elif cmd == "detach":
            # Detach from any observed sessions
            for sid, s in runtime.sessions.items():
                for binding in s.observers:
                    if binding.channel is channel:
                        runtime.detach_observer(sid, channel)
                        console.print(f"[dim]Detached from {sid}[/dim]")
                        return
            console.print("[dim]Not attached to any session.[/dim]")

        elif cmd == "pipe":
            if not args:
                console.print("[red]Usage: /pipe <session_id>[/red]")
                return
            try:
                runtime.connect_pipe(session.id, args)
                console.print(f"[dim]Pipe connected: {session.id} <-> {args}[/dim]")
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")

        elif cmd == "unpipe":
            if not args:
                console.print("[red]Usage: /unpipe <session_id>[/red]")
                return
            try:
                await runtime.disconnect_pipe(session.id, args)
                console.print(f"[dim]Pipe disconnected: {session.id} <-> {args}[/dim]")
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")

        elif cmd == "model":
            if args:
                session.agent_config.model = args
                console.print(f"[dim]Model set to: {args}[/dim]")
            else:
                current = session.agent_config.model or session.agent.default_model
                console.print(f"[dim]Current model: {current}[/dim]")

        elif cmd == "cost":
            if hasattr(session.agent, "get_usage"):
                u = session.agent.get_usage()
                total = u.input_tokens + u.output_tokens
                parts = [f"tokens: {total:,} ({u.input_tokens:,}in + {u.output_tokens:,}out)"]
                if u.total_cost_usd > 0:
                    parts.append(f"cost: ${u.total_cost_usd:.4f}")
                console.print(f"[dim]{' | '.join(parts)}[/dim]")
            else:
                console.print("[dim]No usage data available.[/dim]")

        elif cmd == "rename":
            if not args:
                console.print("[red]Usage: /rename <name>[/red]")
                return
            session.metadata.name = args
            console.print(f"[dim]Session renamed to: {args}[/dim]")

        elif cmd in ("quit", "exit", "q"):
            console.print("[dim]Goodbye![/dim]")
            raise SystemExit(0)

        elif cmd == "effort":
            if args:
                level = args.strip().lower()
                if level not in ("low", "medium", "high"):
                    console.print("[red]Valid effort levels: low, medium, high[/red]")
                    return
                session.agent_config.effort = level
                if hasattr(session.agent, "set_effort"):
                    session.agent.set_effort(level)
                console.print(f"[dim]Effort set to: {level}[/dim]")
            else:
                current = session.agent_config.effort
                if hasattr(session.agent, "get_effort"):
                    current = session.agent.get_effort() or current
                console.print(f"[dim]Current effort: {current}[/dim]")

        elif cmd == "logging":
            if not args:
                current_level = logging.getLevelName(logging.root.level)
                console.print(f"[dim]Current console log level: {current_level}[/dim]")
                return
            level_name = args.upper()
            level = getattr(logging, level_name, None)
            if level is None:
                console.print("[red]Valid levels: DEBUG, INFO, WARNING, ERROR[/red]")
                return
            logging.root.setLevel(level)
            for handler in logging.root.handlers:
                if hasattr(handler, "stream"):
                    handler.setLevel(level)
            console.print(f"[dim]Console log level set to: {level_name}[/dim]")
        else:
            console.print(f"[red]Unknown command: /{cmd}. Type /help for available commands.[/red]")
