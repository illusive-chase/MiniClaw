"""CCTmuxAgent — wraps CC CLI interactive TUI via tmux as an agent backend.

Uses tmux capture-pane/send-keys to drive the CC CLI TUI, converting
its output into AgentProtocol events. This bypasses API gateway blocks
on programmatic modes (SDK, -p, stream-json).
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
from collections.abc import AsyncIterator
from uuid import uuid4

from miniclaw.activity import ActivityEvent, ActivityKind, ActivityStatus
from miniclaw.agent.cc_tmux_parser import ParseEvent, TuiParser, TuiState
from miniclaw.agent.config import AgentConfig
from miniclaw.cancellation import CancellationToken
from miniclaw.interactions import (
    InteractionRequest,
    InteractionResponse,
    InteractionType,
)
from miniclaw.providers.base import ChatMessage
from miniclaw.types import (
    AgentEvent,
    HistoryUpdate,
    TextDelta,
    UsageEvent,
)
from miniclaw.usage import UsageStats

logger = logging.getLogger(__name__)

# Env vars that trigger API gateway blocking — must be cleared.
_BLOCKED_ENV_VARS = [
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDECODE",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
]


class CCTmuxAgent:
    """Agent backend that drives CC CLI interactive TUI via tmux.

    Stateful: maintains a persistent tmux session across messages.
    """

    def __init__(
        self,
        system_prompt: str = "",
        default_model: str = "claude-sonnet-4-6",
        permission_mode: str = "default",
        cwd: str | None = None,
        max_turns: int | None = None,
        claude_bin: str = "claude",
        poll_interval: float = 0.15,
        startup_timeout: float = 30.0,
        idle_timeout: float = 300.0,
        allowed_tools: list[str] | None = None,
        effort: str = "medium",
    ) -> None:
        self._system_prompt = system_prompt
        self._default_model = default_model
        self._permission_mode = permission_mode
        self._cwd = cwd or "."
        self._max_turns = max_turns
        self._claude_bin = claude_bin
        self._poll_interval = poll_interval
        self._startup_timeout = startup_timeout
        self._idle_timeout = idle_timeout
        self._allowed_tools = allowed_tools
        self._effort = effort

        self._session_name: str | None = None
        self._parser = TuiParser()
        self._cumulative_usage = UsageStats()

    # --- AgentProtocol properties ---

    @property
    def agent_type(self) -> str:
        return "ccagent"

    @property
    def default_model(self) -> str:
        return self._default_model

    async def process(
        self,
        text: str,
        history: list[ChatMessage],
        config: AgentConfig,
        token: CancellationToken,
    ) -> AsyncIterator[AgentEvent]:
        """Process a user message via tmux-wrapped CC CLI. Yields AgentEvent items."""
        t0 = time.monotonic()
        logger.info(
            "[CCTmux] process start: text_len=%d, history_len=%d",
            len(text), len(history),
        )

        # Build combined system prompt with plugctx
        plugctx = config.extra.get("_plugctx_prompt", "")
        combined_prompt = "\n\n".join(filter(None, [self._system_prompt, plugctx]))

        try:
            await self._ensure_session(system_prompt=combined_prompt)
            await self._wait_for_idle(self._startup_timeout)
        except Exception as exc:
            error_msg = f"CCTmux session error: {exc}"
            logger.error(error_msg)
            yield TextDelta(error_msg + "\n")
            yield UsageEvent(usage=UsageStats())
            yield HistoryUpdate(history=list(history) + [
                ChatMessage(role="user", content=text),
                ChatMessage(role="assistant", content=error_msg),
            ])
            return

        # Reset parser state for new turn (IDLE since session is already up)
        self._parser.reset(state=TuiState.IDLE)
        # Prime parser with current screen content
        raw = await self._capture_pane()
        self._parser.feed(raw)

        # Send user input
        await self._send_input(text)

        reply_parts: list[str] = []
        pending_tools: dict[str, ActivityEvent] = {}
        turn_usage = UsageStats()
        last_activity = time.monotonic()
        stale_count = 0

        # --- Poll loop ---
        try:
            while True:
                await asyncio.sleep(self._poll_interval)

                # Cancellation check
                if token.is_cancelled:
                    logger.info("[CCTmux] Cancellation detected, sending C-c")
                    await self._send_interrupt()
                    # Brief wait for CC to return to prompt
                    for _ in range(20):
                        await asyncio.sleep(0.2)
                        raw = await self._capture_pane()
                        events = self._parser.feed(raw)
                        if any(e.kind == "idle" for e in events):
                            break
                    break

                raw = await self._capture_pane()
                events = self._parser.feed(raw)

                if events:
                    last_activity = time.monotonic()
                    stale_count = 0
                else:
                    stale_count += 1
                    # Check session health after many stale polls
                    if stale_count % 20 == 0:
                        if not await self._session_alive():
                            yield TextDelta("\n[CCTmux] tmux session lost\n")
                            break
                    # Watchdog timeout
                    if time.monotonic() - last_activity > self._idle_timeout:
                        yield TextDelta("\n[CCTmux] Idle timeout reached\n")
                        break
                    continue

                idle_seen = False
                for event in events:
                    if event.kind == "idle":
                        idle_seen = True
                        break

                    if event.kind == "text":
                        t = event.data["text"] + "\n"
                        reply_parts.append(t)
                        yield TextDelta(t)

                    elif event.kind == "tool_start":
                        tool_id = str(uuid4())
                        ae = ActivityEvent(
                            kind=ActivityKind.TOOL,
                            status=ActivityStatus.START,
                            id=tool_id,
                            name=event.data["tool_name"],
                            summary=event.data.get("args", ""),
                        )
                        pending_tools[event.data["tool_name"]] = ae
                        yield ae

                    elif event.kind == "tool_end":
                        pending = pending_tools.pop(event.data["tool_name"], None)
                        if pending is not None:
                            yield ActivityEvent(
                                kind=ActivityKind.TOOL,
                                status=ActivityStatus.FINISH,
                                id=pending.id,
                                name=pending.name,
                                summary=event.data.get("result", ""),
                            )

                    elif event.kind == "permission":
                        request = self._make_permission_request(event)
                        yield request
                        # Await the channel's response
                        if request._future is not None:
                            try:
                                resp: InteractionResponse = await asyncio.wait_for(
                                    request._future, timeout=120.0
                                )
                                await self._send_permission(resp.allow)
                            except asyncio.TimeoutError:
                                logger.warning("[CCTmux] Permission timeout, auto-deny")
                                await self._send_permission(False)

                    elif event.kind == "error":
                        t = event.data.get("text", "Error") + "\n"
                        reply_parts.append(t)
                        yield TextDelta(t)

                    elif event.kind == "ask_user":
                        loop = asyncio.get_running_loop()
                        future: asyncio.Future[InteractionResponse] = loop.create_future()
                        request = InteractionRequest(
                            id=str(uuid4()),
                            type=InteractionType.ASK_USER,
                            tool_name="AskUserQuestion",
                            tool_input=event.data,
                            _future=future,
                        )
                        yield request
                        try:
                            resp = await asyncio.wait_for(future, timeout=120.0)
                            await self._send_input(resp.message)
                        except asyncio.TimeoutError:
                            logger.warning("[CCTmux] AskUser timeout, sending empty")
                            await self._send_input("")

                    elif event.kind == "plan_approval":
                        loop = asyncio.get_running_loop()
                        future_pa: asyncio.Future[InteractionResponse] = loop.create_future()
                        request = InteractionRequest(
                            id=str(uuid4()),
                            type=InteractionType.PLAN_APPROVAL,
                            tool_name="ExitPlanMode",
                            tool_input=event.data,
                            _future=future_pa,
                        )
                        yield request
                        try:
                            resp = await asyncio.wait_for(future_pa, timeout=120.0)
                            await self._send_permission(resp.allow)
                        except asyncio.TimeoutError:
                            logger.warning("[CCTmux] Plan approval timeout, auto-deny")
                            await self._send_permission(False)

                    elif event.kind == "cost":
                        turn_usage.total_cost_usd += event.data.get("usd", 0.0)

                if idle_seen:
                    break

        except Exception as exc:
            error_msg = f"[CCTmux] Poll error: {exc}"
            logger.error(error_msg, exc_info=True)
            reply_parts.append(error_msg)
            yield TextDelta(error_msg + "\n")

        # Flush any pending tool activities
        for pending in pending_tools.values():
            yield ActivityEvent(
                kind=ActivityKind.TOOL,
                status=ActivityStatus.FINISH,
                id=pending.id,
                name=pending.name,
            )
        pending_tools.clear()

        # --- Finalize ---
        reply = "".join(reply_parts) or "(no response)"
        updated_history = list(history)
        updated_history.append(ChatMessage(role="user", content=text))
        updated_history.append(ChatMessage(role="assistant", content=reply))

        # Merge turn usage into cumulative stats
        self._cumulative_usage.total_cost_usd += turn_usage.total_cost_usd
        self._cumulative_usage.num_turns += 1

        logger.info(
            "[CCTmux] process end: duration_ms=%d, reply_len=%d",
            int((time.monotonic() - t0) * 1000), len(reply),
        )

        yield UsageEvent(usage=turn_usage)
        yield HistoryUpdate(history=updated_history)

    def _make_permission_request(self, event: ParseEvent) -> InteractionRequest:
        """Create an InteractionRequest with a Future for permission resolution."""
        tool_name = event.data.get("tool_name", "unknown")
        raw = event.data.get("raw", "")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[InteractionResponse] = loop.create_future()

        return InteractionRequest(
            id=str(uuid4()),
            type=InteractionType.PERMISSION,
            tool_name=tool_name,
            tool_input={"raw_prompt": raw},
            _future=future,
        )

    # --- Lifecycle methods ---

    async def reset(self) -> None:
        logger.info("[CCTmux] reset: killing session")
        await self._kill_session()
        self._parser.reset()

    async def shutdown(self) -> None:
        logger.info("[CCTmux] shutdown")
        await self._kill_session()

    def serialize_state(self) -> dict:
        return {"tmux_session": self._session_name}

    async def restore_state(self, state: dict) -> None:
        name = state.get("tmux_session")
        if name and await self._check_session(name):
            self._session_name = name
            logger.info("[CCTmux] restored session: %s", name)
        else:
            self._session_name = None

    async def on_fork(self, source_state: dict) -> dict:
        return {}

    # --- Usage & effort ---

    def get_usage(self) -> UsageStats:
        return self._cumulative_usage

    def get_effort(self) -> str:
        return self._effort

    def set_effort(self, level: str) -> None:
        self._effort = level
        # Fire-and-forget: send /effort command to running CC CLI
        if self._session_name:
            asyncio.ensure_future(self._send_input(f"/effort {level}"))

    # --- tmux helpers ---

    async def _run_tmux(self, *args: str, input_data: bytes | None = None) -> str:
        """Run a tmux command and return stdout."""
        proc = await asyncio.create_subprocess_exec(
            "tmux", *args,
            stdin=asyncio.subprocess.PIPE if input_data else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(input=input_data)
        if proc.returncode != 0 and stderr:
            logger.debug("[CCTmux] tmux %s stderr: %s", args[0], stderr.decode(errors="replace"))
        return stdout.decode(errors="replace")

    async def _ensure_session(self, system_prompt: str = "") -> None:
        """Create tmux session and start CC CLI if not already running."""
        if self._session_name and await self._check_session(self._session_name):
            return

        name = f"cc-tmux-{uuid4().hex[:8]}"
        logger.info("[CCTmux] Creating tmux session: %s", name)

        # Create session with large viewport
        await self._run_tmux(
            "new-session", "-d", "-s", name, "-x", "200", "-y", "50",
        )
        # Set large scrollback
        await self._run_tmux("set-option", "-t", name, "history-limit", "50000")

        # Build the CC CLI command with env var clearing
        env_unsets = " ".join(f"-u {v}" for v in _BLOCKED_ENV_VARS)
        cmd_parts = [f"env {env_unsets}"]
        cmd_parts.append(self._claude_bin)
        if self._cwd and self._cwd != ".":
            cmd_parts.extend(["--cwd", shlex.quote(self._cwd)])
        if system_prompt:
            cmd_parts.extend(["--append-system-prompt", shlex.quote(system_prompt)])
        if self._default_model:
            cmd_parts.extend(["--model", shlex.quote(self._default_model)])
        if self._effort:
            cmd_parts.extend(["--effort", shlex.quote(self._effort)])
        if self._permission_mode:
            cmd_parts.extend(["--permission-mode", shlex.quote(self._permission_mode)])
        if self._allowed_tools:
            cmd_parts.extend(["--allowedTools", shlex.quote(",".join(self._allowed_tools))])
        cmd = " ".join(cmd_parts)

        logger.debug("[CCTmux] Starting CC CLI: %s", cmd)
        await self._run_tmux("send-keys", "-t", name, cmd, "Enter")

        self._session_name = name

    async def _wait_for_idle(self, timeout: float) -> None:
        """Wait for the ❯ prompt to appear (CC CLI ready)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = await self._capture_pane()
            if "❯" in raw:
                logger.debug("[CCTmux] Idle prompt detected")
                return
            await asyncio.sleep(0.5)
        raise TimeoutError(
            f"CC CLI did not show ❯ prompt within {timeout}s"
        )

    async def _capture_pane(self) -> str:
        """Capture recent tmux pane content (last 500 lines of scrollback + visible)."""
        if not self._session_name:
            return ""
        return await self._run_tmux(
            "capture-pane", "-t", self._session_name, "-p", "-S", "-500",
        )

    async def _send_input(self, text: str) -> None:
        """Send user text to the CC CLI via tmux."""
        if not self._session_name:
            return

        if "\n" in text or len(text) > 4000:
            # Multi-line or very long: use load-buffer + paste-buffer
            await self._run_tmux(
                "load-buffer", "-", input_data=text.encode()
            )
            await self._run_tmux("paste-buffer", "-t", self._session_name)
            await self._run_tmux("send-keys", "-t", self._session_name, "Enter")
        else:
            # Single-line: send-keys -l (literal mode)
            await self._run_tmux(
                "send-keys", "-t", self._session_name, "-l", text
            )
            await self._run_tmux("send-keys", "-t", self._session_name, "Enter")

    async def _send_permission(self, allow: bool) -> None:
        """Send y/n to a permission prompt."""
        if not self._session_name:
            return
        key = "y" if allow else "n"
        await self._run_tmux("send-keys", "-t", self._session_name, key, "Enter")

    async def _send_interrupt(self) -> None:
        """Send Ctrl-C to abort current operation."""
        if not self._session_name:
            return
        await self._run_tmux("send-keys", "-t", self._session_name, "C-c")

    async def _check_session(self, name: str) -> bool:
        """Check if a tmux session exists."""
        proc = await asyncio.create_subprocess_exec(
            "tmux", "has-session", "-t", name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        return proc.returncode == 0

    async def _session_alive(self) -> bool:
        """Check if our tmux session is still alive."""
        if not self._session_name:
            return False
        return await self._check_session(self._session_name)

    async def _kill_session(self) -> None:
        """Kill the tmux session if it exists."""
        if self._session_name:
            logger.info("[CCTmux] Killing session: %s", self._session_name)
            try:
                await self._run_tmux("kill-session", "-t", self._session_name)
            except Exception:
                pass
            self._session_name = None
