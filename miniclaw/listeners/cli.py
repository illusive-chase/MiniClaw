"""CLIListener — interactive REPL loop for the terminal."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.panel import Panel

from miniclaw.agent.config import AgentConfig
from miniclaw.channels.cli import CLIChannel
from miniclaw.interactions import InteractionRequest, InteractionType
from miniclaw.listeners.base import Listener
from miniclaw.listeners.completer import SlashCommandCompleter
from miniclaw.log import _console
from miniclaw.types import UsageEvent

if TYPE_CHECKING:
    from miniclaw.runtime import Runtime
    from miniclaw.session import Session

logger = logging.getLogger(__name__)


def _print_session_exit(console: Console, session: Session | None) -> None:
    """Print session ID (and name) so the user can /resume later."""
    if session is None:
        return
    sid = session.id
    name = session.metadata.name
    label = f"{sid}  ({name})" if name else sid
    console.print(f"[dim]Session saved: {label}[/dim]")


class CLIListener(Listener):
    """Interactive REPL loop that drives a CLI session.

    Handles:
      - User input via prompt_toolkit
      - Slash commands (/reset, /sessions, /fork, /attach, etc.)
      - SIGINT -> session.interrupt()
      - Message routing via session.submit() + background consumer
      - Stream rendering via CLIChannel
    """

    def __init__(
        self,
        agent_type: str = "native",
        agent_config: AgentConfig | None = None,
        workspace_dir: str = "",
        statusline_config: dict | None = None,
    ) -> None:
        self._agent_type = agent_type
        self._agent_config = agent_config or AgentConfig()
        self._workspace_dir = workspace_dir
        self._session: Session | None = None
        self._statusline_config = statusline_config or {}

    async def run(self, runtime: Runtime) -> None:
        """Main REPL loop."""
        console = _console
        channel = CLIChannel(console=console)

        # Create statusline executor if configured
        sl_script = self._statusline_config.get("script", "")
        if sl_script:
            from miniclaw.statusline import StatusLineExecutor
            self._statusline_executor: StatusLineExecutor | None = StatusLineExecutor(
                sl_script, workspace_dir=self._workspace_dir,
                timeout=self._statusline_config.get("timeout", 2.0),
            )
        else:
            self._statusline_executor = None

        # Create session
        session = runtime.create_session(self._agent_type, self._agent_config)
        session.bind_primary(channel)
        self._session = session

        # Event signaling that the current response is done
        self._response_done = asyncio.Event()
        self._response_done.set()  # start in "ready" state
        self._agent_busy = False

        # Start background consumer
        consume_task = asyncio.create_task(self._consume(session, channel))

        # Setup prompt
        history_path = Path(self._workspace_dir) / ".cli_history"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        completer = SlashCommandCompleter(runtime, session)
        self._completer = completer
        prompt_session: PromptSession = PromptSession(
            history=FileHistory(str(history_path)),
            completer=completer,
            complete_while_typing=False,
        )

        # Install SIGINT handler
        original_handler = signal.getsignal(signal.SIGINT)

        _last_sigint = 0.0

        def _sigint_handler(signum, frame):
            nonlocal _last_sigint
            now = time.monotonic()
            if now - _last_sigint < 2.0:
                raise KeyboardInterrupt  # force exit
            _last_sigint = now
            if self._session is not None:
                self._session.interrupt()

        signal.signal(signal.SIGINT, _sigint_handler)

        console.print(Panel("MiniClaw", subtitle="type /help for commands", style="bold magenta"))

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
                        _print_session_exit(console, self._session)
                        console.print("[dim]Goodbye![/dim]")
                        break

                    # Slash commands
                    if line.startswith("/"):
                        await self._handle_command(
                            line[1:], runtime, session, channel, console
                        )
                        continue

                    # Regular message: submit to queue and wait for response
                    self._response_done.clear()
                    session.submit(line, "user")
                    await self._response_done.wait()

                except EOFError:
                    _print_session_exit(console, self._session)
                    console.print("\n[dim]Goodbye![/dim]")
                    break
                except KeyboardInterrupt:
                    # If the agent is processing in the background (e.g. async
                    # result from a subagent), interrupt it instead of exiting.
                    if self._agent_busy:
                        session.interrupt()
                        continue
                    _print_session_exit(console, self._session)
                    console.print("\n[dim]Goodbye![/dim]")
                    break
        finally:
            signal.signal(signal.SIGINT, original_handler)
            consume_task.cancel()
            try:
                await consume_task
            except asyncio.CancelledError:
                pass

    async def _consume(self, session: Session, channel: CLIChannel) -> None:
        """Background task: consume session.run() and render via channel."""

        async def _refresh_statusline(event: UsageEvent) -> str:
            if self._statusline_executor is not None:
                from miniclaw.statusline import build_statusline_data
                model = session.agent_config.model or getattr(session.agent, "default_model", "unknown")
                data = build_statusline_data(event, model, session.id)
                await self._statusline_executor.refresh(data)
                return self._statusline_executor.text
            return ""

        try:
            async for stream, source in session.run():
                self._agent_busy = True
                await channel.send_stream(stream, on_final_usage=_refresh_statusline)
                self._agent_busy = False
                self._response_done.set()
        except asyncio.CancelledError:
            pass

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
                "  /model [name]     Show or change model\n"
                "  /effort [level]   Show or set thinking effort (low/medium/high)\n"
                "  /cost             Show usage stats\n"
                "  /rename <name>    Rename current session\n"
                "  /logging <level>  Set console log level\n"
                "  /plugctx <cmd>    Manage loaded contexts (init/load/unload/list/status/info)\n"
                "  /remote-check <n> Check remote daemon health\n"
                "  /pwd              Show current working directory\n"
                "  /cd [path]        Change working directory (no args = reset)\n"
                "  /quit, /exit, /q  Exit the REPL",
                title="Help",
                border_style="magenta",
            ))

        elif cmd == "reset":
            # Persist current session before clearing (auto session-dump)
            try:
                runtime.persist_session(session.id)
            except Exception:
                pass  # best-effort; empty history is a no-op anyway
            count = session.clear_history()
            await session.agent.reset()
            console.print(f"[dim]Cleared {count} messages (session dumped).[/dim]")

        elif cmd == "sessions":
            sessions = runtime.list_persisted_sessions()
            if not sessions:
                console.print("[dim]No saved sessions.[/dim]")
            else:
                lines = []
                for s in sessions[:20]:
                    name = s.name or "unnamed"
                    lines.append(f"  {s.id}  {name}  ({s.updated_at})")
                console.print(Panel("\n".join(lines), title="Sessions", border_style="magenta"))

        elif cmd == "resume":
            if not args:
                console.print("[red]Usage: /resume <session_id>[/red]")
                return
            try:
                new_session = await runtime.restore_session(args)
                new_session.bind_primary(channel)
                self._session = new_session
                if self._completer is not None:
                    self._completer.session = new_session
                # Note: the REPL loop still holds the old `session` variable.
                # We update self._session for SIGINT, but commands go through
                # the local var. This is a known limitation.
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
                if self._completer is not None:
                    self._completer.session = forked
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
            _print_session_exit(console, self._session)
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

        elif cmd == "plugctx":
            await self._handle_plugctx(args, session, console)

        elif cmd == "pwd":
            cwd, source = session.effective_cwd()
            console.print(f"[dim]{cwd}[/dim]  [italic]({source})[/italic]")

        elif cmd == "cd":
            if not args:
                session.cwd_override = None
                cwd, source = session.effective_cwd()
                console.print(f"[dim]Reset to {source}: {cwd}[/dim]")
            else:
                target = os.path.expanduser(args.strip())
                base_cwd, _ = session.effective_cwd()
                resolved = os.path.normpath(
                    os.path.join(base_cwd, target) if not os.path.isabs(target) else target
                )
                if not os.path.isdir(resolved):
                    console.print(f"[red]Not a directory: {resolved}[/red]")
                else:
                    session.cwd_override = resolved
                    console.print(f"[dim]cwd: {resolved}[/dim]")

        elif cmd == "remote-check":
            await self._handle_remote_check(args, runtime, console)

        else:
            console.print(f"[red]Unknown command: /{cmd}. Type /help for available commands.[/red]")

    async def _handle_remote_check(
        self,
        args: str,
        runtime: Runtime,
        console: Console,
    ) -> None:
        """Connect to a remote daemon and run a healthcheck."""
        import json

        import aiohttp

        remote = args.strip()
        if not remote:
            console.print("[red]Usage: /remote-check <remote_name>[/red]")
            return

        # Resolve remote name to ws:// URL
        try:
            ws_url = await self._resolve_remote_url(remote, runtime)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            return

        logger.info("Remote check: resolved '%s' -> %s", remote, ws_url)
        console.print(f"[dim]Connecting to {remote} ({ws_url})...[/dim]")

        try:
            async with aiohttp.ClientSession() as http:
                async with http.ws_connect(ws_url, timeout=10) as ws:
                    # Build auth env the same way launch_agent does
                    env: dict[str, str] = {}
                    for key in (
                        "ANTHROPIC_API_KEY",
                        "ANTHROPIC_BASE_URL",
                        "ANTHROPIC_AUTH_TOKEN",
                        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                    ):
                        val = os.environ.get(key)
                        if val:
                            env[key] = val
                    if runtime and runtime.env:
                        env.update(runtime.env)

                    await ws.send_json({"type": "healthcheck", "env": env})
                    logger.debug("Healthcheck request sent to %s", remote)

                    got_result = False
                    async for ws_msg in ws:
                        if ws_msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(ws_msg.data)
                            except json.JSONDecodeError:
                                logger.warning("Malformed JSON from %s: %s", remote, ws_msg.data[:200])
                                console.print(f"[red]Received malformed response from {remote}.[/red]")
                                break
                            if data.get("type") == "healthcheck_result":
                                got_result = True
                                if data.get("ok"):
                                    logger.info("Healthcheck OK for %s", remote)
                                    console.print(
                                        f"[green]OK[/green] — {remote} claude CLI is working.\n"
                                        f"[dim]  output: {data.get('output', '')[:200]}[/dim]"
                                    )
                                else:
                                    logger.info("Healthcheck FAIL for %s: %s", remote, data.get("error", "unknown"))
                                    console.print(
                                        f"[red]FAIL[/red] — {remote} claude CLI check failed.\n"
                                        f"[red]  error: {data.get('error', 'unknown')}[/red]"
                                    )
                                break
                            else:
                                logger.debug("Ignoring message type '%s' from %s", data.get("type"), remote)
                        elif ws_msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                            logger.warning("WebSocket closed unexpectedly during healthcheck for %s", remote)
                            console.print("[red]WebSocket closed unexpectedly.[/red]")
                            break

                    if not got_result:
                        logger.warning("No healthcheck result received from %s (connection ended)", remote)
                        console.print(f"[red]No response received from {remote} — the remote may have disconnected.[/red]")
        except asyncio.TimeoutError:
            logger.warning("Connection to %s timed out", remote)
            console.print(f"[red]Connection to {remote} timed out.[/red]")
        except Exception as e:
            logger.warning("Failed to connect to %s: %s", remote, e)
            console.print(f"[red]Failed to connect to {remote}: {e}[/red]")

    async def _resolve_remote_url(self, remote: str, runtime: Runtime) -> str:
        """Resolve a remote name to a WebSocket URL."""
        if remote.startswith("ws://") or remote.startswith("wss://"):
            return remote

        remotes = getattr(runtime, "_remotes_config", None) or {}
        entry = remotes.get(remote)
        if not entry:
            raise ValueError(
                f"Unknown remote '{remote}'. Configure it under "
                f"'remotes' in config.yaml or pass a ws:// URL directly."
            )

        if isinstance(entry, dict):
            from miniclaw.remote.tunnel import TunnelError

            ssh_host = entry.get("ssh_host")
            if not ssh_host:
                raise ValueError(
                    f"Remote '{remote}' dict config missing 'ssh_host'."
                )
            tunnel_mgr = runtime.tunnel_manager
            try:
                tunnel = await tunnel_mgr.get_or_create(remote, entry)
            except TunnelError as exc:
                raise ValueError(
                    f"Failed to establish SSH tunnel for remote '{remote}': {exc}"
                ) from exc
            return tunnel.ws_url

        return entry

    async def _handle_plugctx(
        self,
        args: str,
        session: Session,
        console: Console,
    ) -> None:
        """Handle /plugctx subcommands."""
        if session.plugctx is None:
            console.print("[red]plugctx is not configured for this session.[/red]")
            return

        parts = args.split(maxsplit=1)
        subcmd = parts[0].lower() if parts else ""
        subargs = parts[1].strip() if len(parts) > 1 else ""

        if subcmd == "load":
            if not subargs:
                console.print("[red]Usage: /plugctx load <dotted.path>[/red]")
                return
            result = session.plugctx.load(subargs)
            if "error" in result:
                console.print(f"[red]{result['error']}[/red]")
                return
            for w in result.get("warnings", []):
                console.print(f"[yellow]{w}[/yellow]")
            if result["loaded"]:
                for p in result["loaded"]:
                    console.print(f"  [green]+[/green] {p}")
            if result["already_loaded"]:
                for p in result["already_loaded"]:
                    console.print(f"  [dim](already loaded) {p}[/dim]")
            if result["failed"]:
                for p in result["failed"]:
                    console.print(f"  [red]! {p} (not found)[/red]")
            console.print(f"[dim]Total context tokens: ~{result['total_tokens']:,}[/dim]")
            if result["children"]:
                console.print(f"[dim]Children: {', '.join(result['children'])}[/dim]")

        elif subcmd == "unload":
            if not subargs:
                console.print("[red]Usage: /plugctx unload <dotted.path>[/red]")
                return
            result = session.plugctx.unload(subargs)
            for w in result.get("warnings", []):
                console.print(f"[yellow]{w}[/yellow]")
            if result["unloaded"]:
                console.print(f"[dim]Unloaded '{result['unloaded']}' (freed ~{result['freed_tokens']:,} tokens)[/dim]")
            else:
                console.print(f"[dim]{result['warnings'][0] if result['warnings'] else 'Nothing to unload.'}[/dim]")

        elif subcmd == "list":
            contexts = session.plugctx.list_contexts()
            if not contexts:
                console.print("[dim]No contexts found under ctx_root.[/dim]")
                return
            lines = []
            for c in contexts:
                marker = "[green]*[/green]" if c["loaded"] else " "
                tokens = f"(~{c['token_estimate']:,} tokens)" if c["token_estimate"] else ""
                lines.append(f"  {marker} {c['path']} {tokens}")
            console.print(Panel("\n".join(lines), title="Contexts", border_style="magenta"))

        elif subcmd == "status":
            result = session.plugctx.status()
            if not result["loaded"]:
                console.print("[dim]No contexts loaded.[/dim]")
                return
            lines = []
            for entry in result["loaded"]:
                desc = f" — {entry['description']}" if entry["description"] else ""
                lines.append(f"  {entry['path']} (~{entry['token_estimate']:,} tokens, {entry['source']}){desc}")
            lines.append(f"\n  Total: ~{result['total_tokens']:,} tokens")
            console.print(Panel("\n".join(lines), title="Loaded Contexts", border_style="magenta"))

        elif subcmd == "info":
            if not subargs:
                console.print("[red]Usage: /plugctx info <dotted.path>[/red]")
                return
            result = session.plugctx.info(subargs)
            if "error" in result:
                console.print(f"[red]{result['error']}[/red]")
                return
            lines = [
                f"  Path:     {result['path']}",
                f"  Loaded:   {'yes' if result['loaded'] else 'no'}",
                f"  Tokens:   ~{result['token_estimate']:,}",
            ]
            if result["name"]:
                lines.append(f"  Name:     {result['name']}")
            if result["description"]:
                lines.append(f"  Desc:     {result['description']}")
            if result["requires"]:
                lines.append(f"  Requires: {', '.join(result['requires'])}")
            if result["tags"]:
                lines.append(f"  Tags:     {', '.join(result['tags'])}")
            if result["children"]:
                lines.append(f"  Children: {', '.join(result['children'])}")
            lines.append(f"\n  Preview:\n{result['preview']}")
            console.print(Panel("\n".join(lines), title=f"Context: {result['path']}", border_style="magenta"))

        elif subcmd == "init":
            await self._handle_plugctx_init(session, console)

        else:
            console.print(
                "[red]Usage: /plugctx <init|load|unload|list|status|info> [args][/red]"
            )

    async def _handle_plugctx_init(
        self,
        session: Session,
        console: Console,
    ) -> None:
        """Interactive scaffold for a new context folder."""
        channel = session.primary_channel
        assert isinstance(channel, CLIChannel)

        # Q1: Context path
        q1 = InteractionRequest(
            id=str(uuid4()),
            type=InteractionType.ASK_USER,
            tool_name="plugctx_init",
            tool_input={
                "questions": [
                    {
                        "question": "Context dotted path? (e.g. project.myapp or skill.coding)",
                        "options": [
                            {"label": "project.<name>", "description": "Project-type context"},
                            {"label": "skill.<name>", "description": "Skill-type context"},
                        ],
                    },
                    {
                        "question": "Context type?",
                        "options": [
                            {"label": "project", "description": "Sets effective working directory for tools"},
                            {"label": "skill", "description": "Only injects prompt content"},
                        ],
                    },
                    {
                        "question": "Dependencies? (comma-separated dotted paths, or 'none')",
                        "options": [
                            {"label": "none", "description": "No dependencies"},
                        ],
                    },
                ],
            },
        )
        r1 = await channel._prompt_ask_user(q1)
        answers = r1.updated_input.get("answers", {}) if r1.updated_input else {}

        dotted_path = answers.get(
            "Context dotted path? (e.g. project.myapp or skill.coding)", ""
        ).strip()
        if not dotted_path:
            console.print("[red]No path provided.[/red]")
            return

        ctx_type = answers.get("Context type?", "skill").strip().lower()
        if ctx_type not in ("project", "skill"):
            ctx_type = "skill"

        deps_raw = answers.get(
            "Dependencies? (comma-separated dotted paths, or 'none')", "none"
        ).strip()
        requires: list[str] = []
        if deps_raw.lower() != "none" and deps_raw:
            requires = [d.strip() for d in deps_raw.split(",") if d.strip()]

        workspace = ""
        if ctx_type == "project":
            q2 = InteractionRequest(
                id=str(uuid4()),
                type=InteractionType.ASK_USER,
                tool_name="plugctx_init",
                tool_input={
                    "questions": [
                        {
                            "question": "Workspace folder path?",
                            "options": [
                                {"label": os.getcwd(), "description": "Current directory"},
                            ],
                        },
                    ],
                },
            )
            r2 = await channel._prompt_ask_user(q2)
            ws_answers = r2.updated_input.get("answers", {}) if r2.updated_input else {}
            workspace = ws_answers.get("Workspace folder path?", os.getcwd()).strip()

        result = session.plugctx.init_context(dotted_path, ctx_type, requires, workspace)
        if result.get("error"):
            console.print(f"[red]Error: {result['error']}[/red]")
        elif result.get("created"):
            console.print(f"[green]Created context '{dotted_path}' at {result['path']}[/green]")
        else:
            console.print(f"[dim]Context not created: {result}[/dim]")
