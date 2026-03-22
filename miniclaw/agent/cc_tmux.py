"""CCTmuxAgent — hook-driven agent backend that wraps CC CLI via tmux.

Uses Claude Code hooks (PreToolUse, PostToolUse, Stop) for structured
tool events and session history files for response text/usage. Tmux is
used only for TTY allocation and input delivery — no screen-scraping.

Architecture:
  Hook bridge (Python script) ←Unix socket→ Hook server (in-process)
  Hook server puts events into asyncio.Queue
  Event loop (async generator) consumes queue, yields AgentEvents
  For PreToolUse interactions: hook handler blocks on a Future that
  the event loop resolves after the Channel answers the user prompt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import stat
import tempfile
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from miniclaw.activity import ActivityEvent, ActivityKind, ActivityStatus
from miniclaw.agent.cc_session_reader import SessionReader
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
    InterruptedEvent,
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

# Hook bridge script — relays CC hook events to MiniClaw agent via Unix socket.
_HOOK_BRIDGE_SCRIPT = r'''#!/usr/bin/env python3
"""Hook bridge — relays CC hook events to MiniClaw agent via Unix socket."""
import json, os, socket, sys

sock_path = os.environ.get("MINICLAW_HOOK_SOCK", "")
if not sock_path:
    json.dump({}, sys.stdout)
    sys.exit(0)

try:
    data = json.loads(sys.stdin.read())
except Exception:
    data = {}

try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(600)
        s.connect(sock_path)
        s.sendall(json.dumps(data).encode() + b"\n")
        chunks = []
        while True:
            chunk = s.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
        response = json.loads(b"".join(chunks).decode())
    json.dump(response, sys.stdout)
except Exception as e:
    sys.stderr.write(f"hook_bridge error: {e}\n")
    json.dump({}, sys.stdout)
'''

# Queue item tags
_TAG_STOP = "stop"
_TAG_POST_TOOL = "post_tool"
_TAG_PRE_TOOL = "pre_tool"        # auto-allowed, no interaction needed
_TAG_INTERACTION = "interaction"  # needs user interaction before responding to hook


class CCTmuxAgent:
    """Agent backend that drives CC CLI via tmux with hook-based event capture.

    Per-turn lifecycle:
    1. Start CC CLI in tmux with --session-id (or --resume) and --settings
    2. Send user message via tmux send-keys
    3. Hooks fire on Unix domain socket: PreToolUse, PostToolUse, Stop
    4. On Stop: send /exit, read session JSONL for text/usage
    5. Yield AgentEvents
    6. Next turn: restart CC CLI with --resume
    """

    def __init__(
        self,
        system_prompt: str = "",
        default_model: str = "claude-sonnet-4-6",
        permission_mode: str = "default",
        cwd: str | None = None,
        max_turns: int | None = None,
        claude_bin: str = "claude",
        startup_timeout: float = 60.0,
        allowed_tools: list[str] | None = None,
        effort: str = "medium",
    ) -> None:
        self._system_prompt = system_prompt
        self._default_model = default_model
        self._permission_mode = permission_mode
        self._cwd = cwd or os.getcwd()
        self._max_turns = max_turns
        self._claude_bin = claude_bin
        self._startup_timeout = startup_timeout
        self._allowed_tools = allowed_tools
        self._effort = effort

        self._cc_session_id: str | None = None
        self._line_watermark: int = 0
        self._tmux_session: str | None = None
        self._temp_dir: str | None = None
        self._cumulative_usage = UsageStats()

    # --- AgentProtocol properties ---

    @property
    def agent_type(self) -> str:
        return "ccagent"

    @property
    def backend(self) -> str:
        return "cctmux"

    @property
    def default_model(self) -> str:
        return self._default_model

    # --- Main processing ---

    async def process(
        self,
        text: str,
        history: list[ChatMessage],
        config: AgentConfig,
        token: CancellationToken,
    ) -> AsyncIterator[AgentEvent]:
        """Process a user message via hook-driven CC CLI. Yields AgentEvent items."""
        t0 = time.monotonic()
        logger.info(
            "[CCTmux] process start: text_len=%d, history_len=%d",
            len(text), len(history),
        )

        # Determine session ID
        if not self._cc_session_id:
            self._cc_session_id = str(uuid4())
            self._line_watermark = 0
        is_resume = self._line_watermark > 0

        # Prepare temp dir with hook bridge and settings
        temp_dir = self._ensure_temp_dir()
        sock_path = os.path.join(temp_dir, "hook.sock")

        # Build combined system prompt
        plugctx = config.extra.get("_plugctx_prompt", "")
        if plugctx:
            from miniclaw.plugctx.vpath import resolve_virtual_paths
            path_ctx = config.extra.get("_path_ctx")
            plugctx = resolve_virtual_paths(
                plugctx,
                ctx_root=path_ctx.ctx_root if path_ctx else None,
                workspace=path_ctx.workspace if path_ctx else None,
            )
        combined_prompt = "\n\n".join(filter(None, [self._system_prompt, plugctx]))

        # Event queue consumed by the event loop
        hook_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        stop_event = asyncio.Event()
        server: asyncio.AbstractServer | None = None
        interrupted = False

        try:
            # Start Unix socket server for hooks
            server = await self._start_hook_server(
                sock_path, hook_queue, stop_event,
            )

            # Start CC CLI in tmux
            await self._start_cc_cli(
                system_prompt=combined_prompt,
                is_resume=is_resume,
                sock_path=sock_path,
            )

            # Wait for CC CLI to start up
            await self._wait_for_startup()

            # On resume, CC CLI replays the old /exit command and fires a
            # spurious Stop hook + "No response requested." cycle.  Wait for
            # that to settle, drain the queue, then proceed.
            if is_resume:
                await self._drain_resume_stop(hook_queue, stop_event)

            # Send user message
            await self._send_input(text)

            # Event loop: process hooks until Stop
            async for event in self._event_loop(hook_queue, stop_event, token):
                if isinstance(event, InterruptedEvent):
                    interrupted = True
                yield event

        except Exception as exc:
            error_msg = f"[CCTmux] Error: {exc}"
            logger.error(error_msg, exc_info=True)
            yield TextDelta(error_msg + "\n")

        finally:
            if server:
                server.close()
                await server.wait_closed()
            try:
                os.unlink(sock_path)
            except OSError:
                pass

        # Send /exit and wait for tmux session to end
        if not interrupted:
            await self._exit_cc_cli()

        # Read session file for results
        reader = SessionReader(self._cwd, self._cc_session_id)
        turn_result = reader.read_new_messages(after_line=self._line_watermark)
        self._line_watermark = turn_result.watermark

        # Yield text from session file
        if turn_result.assistant_text:
            yield TextDelta(turn_result.assistant_text)

        # Build history
        reply = turn_result.assistant_text or "(no response)"
        updated_history = list(history)
        updated_history.append(ChatMessage(role="user", content=text))
        updated_history.append(ChatMessage(role="assistant", content=reply))

        # Accumulate usage
        self._cumulative_usage.input_tokens += turn_result.usage.input_tokens
        self._cumulative_usage.output_tokens += turn_result.usage.output_tokens
        self._cumulative_usage.cache_read_tokens += turn_result.usage.cache_read_tokens
        self._cumulative_usage.cache_creation_tokens += turn_result.usage.cache_creation_tokens
        self._cumulative_usage.num_turns += 1
        self._cumulative_usage.num_requests += turn_result.usage.num_requests

        logger.info(
            "[CCTmux] process end: duration_ms=%d, reply_len=%d, "
            "input=%d, output=%d, cache_read=%d",
            int((time.monotonic() - t0) * 1000),
            len(reply),
            turn_result.usage.input_tokens,
            turn_result.usage.output_tokens,
            turn_result.usage.cache_read_tokens,
        )

        yield UsageEvent(usage=turn_result.usage)
        yield HistoryUpdate(history=updated_history)

    # --- Event loop ---

    async def _event_loop(
        self,
        hook_queue: asyncio.Queue[tuple[str, dict[str, Any]]],
        stop_event: asyncio.Event,
        token: CancellationToken,
    ) -> AsyncIterator[AgentEvent]:
        """Consume hook events, yield AgentEvents until Stop or cancellation."""
        pending_tools: dict[str, ActivityEvent] = {}

        while not stop_event.is_set():
            # Check cancellation
            if token.is_cancelled:
                logger.info("[CCTmux] Cancellation detected")
                await self._send_interrupt()
                await asyncio.sleep(1.0)
                await self._exit_cc_cli()
                for p in pending_tools.values():
                    yield ActivityEvent(
                        kind=ActivityKind.TOOL, status=ActivityStatus.FINISH,
                        id=p.id, name=p.name,
                    )
                yield InterruptedEvent()
                return

            # Wait for next item
            try:
                tag, data = await asyncio.wait_for(hook_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if not await self._session_alive():
                    logger.warning("[CCTmux] tmux session died unexpectedly")
                    break
                continue

            # --- Stop ---
            if tag == _TAG_STOP:
                logger.debug("[CCTmux] Stop received")
                break

            # --- PostToolUse ---
            if tag == _TAG_POST_TOOL:
                tool_id = data.get("tool_use_id", "")
                pending = pending_tools.pop(tool_id, None)
                if pending is not None:
                    yield ActivityEvent(
                        kind=ActivityKind.TOOL,
                        status=ActivityStatus.FINISH,
                        id=pending.id,
                        name=pending.name,
                        summary=_truncate(str(data.get("tool_result", "")), 200),
                    )
                continue

            # --- PreToolUse (auto-allowed) ---
            if tag == _TAG_PRE_TOOL:
                tool_id = data.get("tool_use_id", str(uuid4()))
                ae = ActivityEvent(
                    kind=ActivityKind.TOOL,
                    status=ActivityStatus.START,
                    id=tool_id,
                    name=data.get("tool_name", ""),
                    summary=_truncate(str(data.get("tool_input", {})), 200),
                )
                pending_tools[tool_id] = ae
                yield ae
                continue

            # --- PreToolUse (interaction needed) ---
            if tag == _TAG_INTERACTION:
                request: InteractionRequest = data["request"]
                hook_future: asyncio.Future[dict] = data["hook_future"]
                tool_name = request.tool_name

                # Yield START activity
                tool_id = data.get("tool_use_id", str(uuid4()))
                ae = ActivityEvent(
                    kind=ActivityKind.TOOL,
                    status=ActivityStatus.START,
                    id=tool_id,
                    name=tool_name,
                    summary=_truncate(str(request.tool_input), 200),
                )
                pending_tools[tool_id] = ae
                yield ae

                # Yield InteractionRequest so Channel shows prompt to user
                yield request

                # Wait for user response (Channel resolves request._future)
                try:
                    resp: InteractionResponse = await asyncio.wait_for(
                        request._future, timeout=120.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("[CCTmux] Interaction timeout for %s", tool_name)
                    resp = InteractionResponse(id=request.id, allow=False, message="Timed out")

                # Compute hook response and unblock the hook handler
                hook_resp = self._build_hook_response(request.type, tool_name, resp)
                hook_future.set_result(hook_resp)

                # For AskUserQuestion: schedule answer delivery after allow
                if request.type == InteractionType.ASK_USER and resp.allow:
                    answer = resp.message or ""
                    asyncio.get_running_loop().call_later(
                        1.0,
                        lambda a=answer: asyncio.ensure_future(self._send_input(a)),
                    )
                continue

        # Flush remaining pending tools
        for p in pending_tools.values():
            yield ActivityEvent(
                kind=ActivityKind.TOOL, status=ActivityStatus.FINISH,
                id=p.id, name=p.name,
            )

    @staticmethod
    def _build_hook_response(
        itype: InteractionType,
        tool_name: str,
        resp: InteractionResponse,
    ) -> dict:
        """Build the JSON response to send back to the hook bridge."""
        if resp.allow:
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": resp.message or f"Denied: {tool_name}",
            }
        }

    # --- Hook server ---

    def _start_hook_server(
        self,
        sock_path: str,
        hook_queue: asyncio.Queue[tuple[str, dict[str, Any]]],
        stop_event: asyncio.Event,
    ):
        """Start a Unix domain socket server that receives hook events.

        Returns a coroutine that resolves to the server.
        """
        agent = self  # capture for closures

        async def handle_connection(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
        ) -> None:
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=600)
                if not raw:
                    writer.close()
                    return

                hook_data = json.loads(raw.decode())
                # CC CLI sends flat JSON with hook_event_name at top level
                hook_name = hook_data.get("hook_event_name", "")
                tool_name = hook_data.get("tool_name", "")
                tool_input = hook_data.get("tool_input", {})
                tool_use_id = hook_data.get("tool_use_id", "")

                response: dict

                if hook_name == "Stop":
                    stop_event.set()
                    await hook_queue.put((_TAG_STOP, {}))
                    response = {}

                elif hook_name == "PostToolUse":
                    await hook_queue.put((_TAG_POST_TOOL, {
                        "tool_name": tool_name,
                        "tool_use_id": tool_use_id,
                        "tool_result": hook_data.get("tool_response", ""),
                    }))
                    response = {}

                elif hook_name == "PreToolUse":
                    response = await agent._handle_pre_tool_use(
                        tool_name, tool_input, tool_use_id, hook_queue,
                    )

                else:
                    response = {}

                writer.write(json.dumps(response).encode())
                await writer.drain()

            except Exception as exc:
                logger.error("[CCTmux] Hook error: %s", exc, exc_info=True)
                try:
                    writer.write(json.dumps({}).encode())
                    await writer.drain()
                except Exception:
                    pass
            finally:
                try:
                    writer.close()
                except Exception:
                    pass

        return asyncio.start_unix_server(handle_connection, path=sock_path)

    async def _handle_pre_tool_use(
        self,
        tool_name: str,
        tool_input: dict,
        tool_use_id: str,
        hook_queue: asyncio.Queue[tuple[str, dict[str, Any]]],
    ) -> dict:
        """Handle a PreToolUse hook event.

        For auto-allowed tools: puts a _TAG_PRE_TOOL item in the queue and
        returns immediately with allow.

        For tools needing interaction: creates an InteractionRequest with a
        Future, puts it in the queue as _TAG_INTERACTION along with a
        hook_future, and blocks until the event loop resolves hook_future.
        """
        # Auto-allow check
        if self._is_auto_allowed(tool_name):
            await hook_queue.put((_TAG_PRE_TOOL, {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_use_id": tool_use_id,
            }))
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }}

        # Determine interaction type
        if tool_name == "AskUserQuestion":
            itype = InteractionType.ASK_USER
        elif tool_name == "ExitPlanMode":
            itype = InteractionType.PLAN_APPROVAL
        else:
            itype = InteractionType.PERMISSION

        loop = asyncio.get_running_loop()

        # Future for the user's InteractionResponse (resolved by Channel)
        user_future: asyncio.Future[InteractionResponse] = loop.create_future()

        request = InteractionRequest(
            id=str(uuid4()),
            type=itype,
            tool_name=tool_name,
            tool_input=tool_input,
            _future=user_future,
        )

        # Future for the hook response (resolved by event loop after user answers)
        hook_future: asyncio.Future[dict] = loop.create_future()

        await hook_queue.put((_TAG_INTERACTION, {
            "request": request,
            "hook_future": hook_future,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_use_id": tool_use_id,
        }))

        # Block until event loop processes the interaction and resolves hook_future
        try:
            return await asyncio.wait_for(hook_future, timeout=180.0)
        except asyncio.TimeoutError:
            logger.warning("[CCTmux] Hook handler timed out for %s", tool_name)
            return {
                "hookSpecificOutput": {
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "Timed out",
                }
            }

    def _is_auto_allowed(self, tool_name: str) -> bool:
        """Check if a tool is auto-allowed based on current permission config."""
        if self._allowed_tools is not None:
            return tool_name in self._allowed_tools
        if self._permission_mode in ("plan", "bypasstool"):
            return True
        return False

    # --- Temp dir and hook files ---

    def _ensure_temp_dir(self) -> str:
        """Create temp directory with hook bridge script and settings."""
        if self._temp_dir and os.path.isdir(self._temp_dir):
            return self._temp_dir

        temp_dir = tempfile.mkdtemp(prefix=f"miniclaw-{self._cc_session_id[:8]}-")
        self._temp_dir = temp_dir

        # Write hook bridge script
        bridge_path = os.path.join(temp_dir, "hook_bridge.py")
        with open(bridge_path, "w") as f:
            f.write(_HOOK_BRIDGE_SCRIPT)
        os.chmod(bridge_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)

        # Write hooks settings JSON
        settings = self._build_hook_settings(bridge_path)
        settings_path = os.path.join(temp_dir, "hooks_settings.json")
        with open(settings_path, "w") as f:
            json.dump(settings, f)

        logger.debug("[CCTmux] Temp dir created: %s", temp_dir)
        return temp_dir

    def _build_hook_settings(self, bridge_path: str) -> dict:
        """Generate the hooks settings JSON for CC CLI."""
        bridge_cmd = f"python3 {shlex.quote(bridge_path)}"
        return {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": ".*",
                        "hooks": [{"type": "command", "command": bridge_cmd}],
                    }
                ],
                "PostToolUse": [
                    {
                        "matcher": ".*",
                        "hooks": [{"type": "command", "command": bridge_cmd}],
                    }
                ],
                "Stop": [
                    {
                        "hooks": [{"type": "command", "command": bridge_cmd}],
                    }
                ],
            }
        }

    # --- CC CLI lifecycle ---

    async def _start_cc_cli(
        self,
        system_prompt: str,
        is_resume: bool,
        sock_path: str,
    ) -> None:
        """Start CC CLI in a tmux session."""
        if self._tmux_session:
            await self._kill_tmux()

        name = f"cc-tmux-{uuid4().hex[:8]}"
        logger.info("[CCTmux] Creating tmux session: %s", name)

        # Create tmux session
        await self._run_tmux("new-session", "-d", "-s", name, "-x", "200", "-y", "50")
        await self._run_tmux("set-option", "-t", name, "history-limit", "50000")

        # Set MINICLAW_HOOK_SOCK in tmux environment (inherited by child processes)
        await self._run_tmux(
            "set-environment", "-t", name, "MINICLAW_HOOK_SOCK", sock_path,
        )
        # Clear blocked env vars
        for var in _BLOCKED_ENV_VARS:
            await self._run_tmux("set-environment", "-t", name, "-u", var)

        # Build CC CLI command
        settings_path = os.path.join(self._temp_dir, "hooks_settings.json")
        cmd_parts = ["env"]
        for var in _BLOCKED_ENV_VARS:
            cmd_parts.append(f"-u {var}")
        cmd_parts.append(f"MINICLAW_HOOK_SOCK={shlex.quote(sock_path)}")
        cmd_parts.append(self._claude_bin)

        if is_resume:
            cmd_parts.extend(["--resume", self._cc_session_id])
        else:
            cmd_parts.extend(["--session-id", self._cc_session_id])

        cmd_parts.extend(["--settings", shlex.quote(settings_path)])

        if system_prompt and not is_resume:
            prompt_path = os.path.join(self._temp_dir, "system_prompt.txt")
            with open(prompt_path, "w") as f:
                f.write(system_prompt)
            cmd_parts.extend(["--append-system-prompt-file", shlex.quote(prompt_path)])
        if self._default_model:
            cmd_parts.extend(["--model", shlex.quote(self._default_model)])
        if self._effort:
            cmd_parts.extend(["--effort", shlex.quote(self._effort)])
        if self._permission_mode:
            cmd_parts.extend(["--permission-mode", shlex.quote(self._permission_mode)])
        if self._allowed_tools:
            cmd_parts.extend(
                ["--allowedTools", shlex.quote(",".join(self._allowed_tools))]
            )

        cmd = " ".join(cmd_parts)
        if self._cwd and self._cwd != ".":
            cmd = f"cd {shlex.quote(self._cwd)} && {cmd}"
        logger.debug("[CCTmux] Starting CC CLI: %s", cmd)
        await self._run_tmux("send-keys", "-t", name, cmd, "Enter")

        self._tmux_session = name

    async def _wait_for_startup(self) -> None:
        """Wait for CC CLI to show the interactive prompt."""
        deadline = time.monotonic() + self._startup_timeout
        while time.monotonic() < deadline:
            raw = await self._capture_pane()
            if "❯" in raw:
                logger.debug("[CCTmux] CC CLI ready (prompt detected)")
                return
            await asyncio.sleep(0.5)
        raise TimeoutError(
            f"CC CLI did not show ❯ prompt within {self._startup_timeout}s"
        )

    async def _drain_resume_stop(
        self,
        hook_queue: asyncio.Queue[tuple[str, dict[str, Any]]],
        stop_event: asyncio.Event,
    ) -> None:
        """Wait for and discard the spurious Stop hook that fires on resume.

        When CC CLI resumes a session whose last messages include /exit,
        it generates a synthetic "No response requested." reply and fires
        a Stop hook before returning to the ❯ prompt.  We must consume
        that Stop so it doesn't short-circuit the real event loop.
        """
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if stop_event.is_set():
                break
            try:
                tag, _ = await asyncio.wait_for(hook_queue.get(), timeout=1.0)
                if tag == _TAG_STOP:
                    break
            except asyncio.TimeoutError:
                continue

        # Clear the stop_event and drain any remaining queued items
        stop_event.clear()
        while not hook_queue.empty():
            try:
                hook_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        # Wait for the ❯ prompt again (CC CLI returns to prompt after the cycle)
        await self._wait_for_startup()
        logger.debug("[CCTmux] Resume stop cycle drained, ready for new input")

    async def _exit_cc_cli(self) -> None:
        """Send /exit to CC CLI and wait for tmux session to end."""
        if not self._tmux_session:
            return

        logger.debug("[CCTmux] Sending /exit")
        await self._run_tmux("send-keys", "-t", self._tmux_session, "-l", "/exit")
        await self._run_tmux("send-keys", "-t", self._tmux_session, "Enter")

        # Wait for tmux session to die
        for _ in range(30):
            await asyncio.sleep(0.5)
            if not await self._session_alive():
                logger.debug("[CCTmux] tmux session ended cleanly")
                self._tmux_session = None
                return

        logger.warning("[CCTmux] tmux session did not exit, force killing")
        await self._kill_tmux()

    async def _send_input(self, text: str) -> None:
        """Send user text to CC CLI via tmux."""
        if not self._tmux_session:
            return

        await self._run_tmux("load-buffer", "-", input_data=text.encode())
        await self._run_tmux("paste-buffer", "-t", self._tmux_session)
        time.sleep(5)
        await self._run_tmux("send-keys", "-t", self._tmux_session, "Enter")

    async def _send_interrupt(self) -> None:
        """Send Ctrl-C to abort current operation."""
        if not self._tmux_session:
            return
        await self._run_tmux("send-keys", "-t", self._tmux_session, "C-c")

    # --- Lifecycle methods ---

    async def reset(self) -> None:
        """Discard internal state."""
        logger.info("[CCTmux] reset")
        await self._kill_tmux()
        self._cc_session_id = None
        self._line_watermark = 0

    async def shutdown(self) -> None:
        """Release all resources."""
        logger.info("[CCTmux] shutdown")
        await self._kill_tmux()
        self._cleanup_temp_dir()

    def serialize_state(self) -> dict:
        return {
            "cc_session_id": self._cc_session_id,
            "line_watermark": self._line_watermark,
        }

    async def restore_state(self, state: dict) -> None:
        self._cc_session_id = state.get("cc_session_id")
        self._line_watermark = state.get("line_watermark", 0)
        if self._cc_session_id:
            logger.info(
                "[CCTmux] Restored state: session=%s, watermark=%d",
                self._cc_session_id, self._line_watermark,
            )

    async def on_fork(self, source_state: dict) -> dict:
        return {}

    # --- Usage & effort ---

    def get_usage(self) -> UsageStats:
        return self._cumulative_usage

    def get_effort(self) -> str:
        return self._effort

    def set_effort(self, level: str) -> None:
        self._effort = level

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
            logger.debug(
                "[CCTmux] tmux %s stderr: %s",
                args[0], stderr.decode(errors="replace"),
            )
        return stdout.decode(errors="replace")

    async def _capture_pane(self) -> str:
        """Capture tmux pane content."""
        if not self._tmux_session:
            return ""
        return await self._run_tmux(
            "capture-pane", "-t", self._tmux_session, "-p", "-S", "-500",
        )

    async def _session_alive(self) -> bool:
        """Check if the tmux session is still alive."""
        if not self._tmux_session:
            return False
        proc = await asyncio.create_subprocess_exec(
            "tmux", "has-session", "-t", self._tmux_session,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        return proc.returncode == 0

    async def _kill_tmux(self) -> None:
        """Kill the tmux session if it exists."""
        if self._tmux_session:
            logger.info("[CCTmux] Killing tmux session: %s", self._tmux_session)
            try:
                await self._run_tmux("kill-session", "-t", self._tmux_session)
            except Exception:
                pass
            self._tmux_session = None

    def _cleanup_temp_dir(self) -> None:
        """Remove temp directory."""
        if self._temp_dir and os.path.isdir(self._temp_dir):
            try:
                shutil.rmtree(self._temp_dir)
                logger.debug("[CCTmux] Cleaned up temp dir: %s", self._temp_dir)
            except OSError as exc:
                logger.warning("[CCTmux] Failed to clean temp dir: %s", exc)
            self._temp_dir = None


def _truncate(s: str, maxlen: int = 200) -> str:
    """Truncate a string for logging."""
    return s[:maxlen] + "..." if len(s) > maxlen else s
