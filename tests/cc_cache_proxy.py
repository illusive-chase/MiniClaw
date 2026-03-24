#!/usr/bin/env python3
"""Standalone cache debug proxy for official Claude Code.

Transparent HTTP proxy between Claude Code and the Anthropic API that logs
full request/response payloads with a focus on cache-related fields.

Usage:
    # Terminal 1: start proxy
    python tests/cc_cache_proxy.py --port 8080

    # Terminal 2: run official Claude Code through it
    ANTHROPIC_BASE_URL=http://localhost:8080 claude

No MiniClaw imports — only stdlib + aiohttp.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

# ─── ANSI helpers ───────────────────────────────────────────────────────────

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_BLUE = "\033[34m"
_CYAN = "\033[36m"
_MAGENTA = "\033[35m"


def _c(text: str, *codes: str) -> str:
    return "".join(codes) + str(text) + _RESET


# ─── Argument parsing ──────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Cache debug proxy for Claude Code ↔ Anthropic API.",
    )
    p.add_argument("--port", type=int, default=8080, help="Proxy listen port (default: 8080)")
    p.add_argument(
        "--log-file",
        default=".workspace/temp_cc_cache_proxy.jsonl",
        help="JSONL log file path (default: .workspace/temp_cc_cache_proxy.jsonl)",
    )
    p.add_argument(
        "--upstream",
        help="Upstream base URL (default: $ANTHROPIC_BASE_URL)",
    )
    p.add_argument("--verbose", action="store_true", help="Print full request/response bodies to stderr")
    args = p.parse_args()
    if args.upstream is None:
        args.upstream = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    return args


# ─── Cache breakpoint extraction ───────────────────────────────────────────

def extract_cache_breakpoints(body: dict) -> list[dict[str, str]]:
    """Walk the request JSON and find all cache_control markers."""
    breakpoints: list[dict[str, str]] = []

    # System blocks
    system = body.get("system")
    if isinstance(system, list):
        for i, block in enumerate(system):
            if isinstance(block, dict) and "cache_control" in block:
                breakpoints.append({
                    "location": f"system[{i}]",
                    "type": block["cache_control"].get("type", "unknown"),
                })
    elif isinstance(system, str):
        pass  # plain string, no cache_control possible

    # Tools
    tools = body.get("tools")
    if isinstance(tools, list):
        for i, tool in enumerate(tools):
            if isinstance(tool, dict) and "cache_control" in tool:
                breakpoints.append({
                    "location": f"tools[{i}]",
                    "type": tool["cache_control"].get("type", "unknown"),
                })

    # Messages
    messages = body.get("messages")
    if isinstance(messages, list):
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            # Top-level cache_control on message
            if "cache_control" in msg:
                breakpoints.append({
                    "location": f"messages[{i}]",
                    "type": msg["cache_control"].get("type", "unknown"),
                })
            # Content blocks
            content = msg.get("content")
            if isinstance(content, list):
                for j, block in enumerate(content):
                    if isinstance(block, dict) and "cache_control" in block:
                        breakpoints.append({
                            "location": f"messages[{i}].content[{j}]",
                            "type": block["cache_control"].get("type", "unknown"),
                        })

    return breakpoints


# ─── SSE Accumulator ───────────────────────────────────────────────────────

class SSEAccumulator:
    """Accumulate SSE chunks, parse events, extract usage stats."""

    def __init__(self) -> None:
        self._buffer = b""
        self.event_count = 0
        self.usage: dict[str, int] = {}
        self.model: str | None = None

    def feed(self, chunk: bytes) -> None:
        """Feed raw bytes and parse any complete SSE lines."""
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._process_line(line)

    def flush(self) -> None:
        """Process any remaining data in the buffer."""
        if self._buffer.strip():
            self._process_line(self._buffer)
            self._buffer = b""

    def _process_line(self, line: bytes) -> None:
        try:
            text = line.decode("utf-8", errors="replace").strip()
        except Exception:
            return
        if not text.startswith("data: "):
            return
        payload = text[6:]
        if payload == "[DONE]":
            return
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return
        self.event_count += 1
        event_type = data.get("type", "")

        if event_type == "message_start":
            msg = data.get("message", {})
            self.model = msg.get("model")
            usage = msg.get("usage", {})
            self._merge_usage(usage)
        elif event_type == "message_delta":
            usage = data.get("usage", {})
            self._merge_usage(usage)

    def _merge_usage(self, usage: dict) -> None:
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            if key in usage:
                self.usage[key] = usage[key]


# ─── JSONL Logger ──────────────────────────────────────────────────────────

_REDACT_HEADERS = {"x-api-key", "authorization"}


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    out = {}
    for k, v in headers.items():
        if k.lower() in _REDACT_HEADERS:
            out[k] = v[:12] + "..." if len(v) > 12 else "REDACTED"
        else:
            out[k] = v
    return out


class JsonlLogger:
    """Append request/response records to a JSONL file."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._seq = 0

    def log(
        self,
        *,
        method: str,
        path: str,
        req_headers: dict[str, str],
        req_body: dict | None,
        cache_breakpoints: list[dict[str, str]],
        status: int,
        resp_headers: dict[str, str],
        usage: dict[str, int],
        sse_event_count: int,
        elapsed_ms: int,
    ) -> None:
        self._seq += 1
        record = {
            "seq": self._seq,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": elapsed_ms,
            "request": {
                "method": method,
                "path": path,
                "headers": _redact_headers(req_headers),
                "body": req_body,
                "cache_breakpoints": cache_breakpoints,
            },
            "response": {
                "status": status,
                "headers": {
                    k: v
                    for k, v in resp_headers.items()
                    if k.lower() in ("request-id", "x-request-id", "cf-ray")
                },
                "usage": usage,
                "sse_event_count": sse_event_count,
            },
        }
        with self._path.open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ─── Console summary ──────────────────────────────────────────────────────

def print_summary(
    seq: int,
    method: str,
    path: str,
    status: int,
    elapsed_ms: int,
    model: str | None,
    breakpoints: list[dict[str, str]],
    usage: dict[str, int],
) -> None:
    """Print ANSI-colored per-request summary to stderr."""
    # Header bar
    status_color = _GREEN if 200 <= status < 300 else _RED
    print(
        f"\n{_c('━' * 3, _DIM)} {_c(f'#{seq}', _BOLD, _CYAN)} "
        f"{_c(method, _BOLD)} {_c(path, _BLUE)} "
        f"{_c('━' * 3, _DIM)} {_c(str(status), _BOLD, status_color)} "
        f"{_c('━' * 3, _DIM)} {_c(f'{elapsed_ms}ms', _YELLOW)} "
        f"{_c('━' * 3, _DIM)}",
        file=sys.stderr,
    )

    if model:
        print(f"  Model: {_c(model, _BOLD)}", file=sys.stderr)

    if breakpoints:
        locs = ", ".join(bp["location"] for bp in breakpoints)
        print(f"  Cache breakpoints: {_c(locs, _MAGENTA)}", file=sys.stderr)
    else:
        print(f"  Cache breakpoints: {_c('(none)', _DIM)}", file=sys.stderr)

    in_tok = usage.get("input_tokens", 0)
    out_tok = usage.get("output_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_create = usage.get("cache_creation_input_tokens", 0)

    print(
        f"  Tokens:  in={_c(in_tok, _BOLD)}  out={_c(out_tok, _BOLD)}  "
        f"cache_read={_c(cache_read, _BOLD, _GREEN)}  "
        f"cache_create={_c(cache_create, _BOLD, _YELLOW)}",
        file=sys.stderr,
    )

    # Cache hit bar
    total_input = in_tok + cache_read + cache_create
    if total_input > 0 and (cache_read or cache_create):
        hit_pct = cache_read / total_input * 100
        bar_len = 24
        filled = int(bar_len * hit_pct / 100)
        bar = "\u2588" * filled + "\u2591" * (bar_len - filled)
        color = _GREEN if hit_pct > 80 else (_YELLOW if hit_pct > 40 else _RED)
        print(
            f"  Cache:   {_c(bar, color)} {_c(f'{hit_pct:.1f}%', _BOLD, color)} hit",
            file=sys.stderr,
        )

    sys.stderr.flush()


# ─── Request handler ───────────────────────────────────────────────────────

# Headers NOT forwarded to the client (aiohttp manages these)
_HOP_BY_HOP = {"transfer-encoding", "content-length", "content-encoding"}


def _make_handler(
    upstream: str,
    logger: JsonlLogger,
    verbose: bool,
) -> Any:
    seq_counter = {"n": 0}

    async def handle_request(request: web.Request) -> web.StreamResponse:
        seq_counter["n"] += 1
        seq = seq_counter["n"]
        method = request.method
        path = request.path_qs  # includes query string

        # Read raw body
        raw_body = await request.read()
        req_body: dict | None = None
        breakpoints: list[dict[str, str]] = []
        is_json = False

        if raw_body:
            try:
                req_body = json.loads(raw_body)
                is_json = True
                breakpoints = extract_cache_breakpoints(req_body)
            except (json.JSONDecodeError, ValueError):
                pass

        req_headers = dict(request.headers)

        if verbose and is_json:
            print(
                f"\n{_c('>>> REQUEST', _BOLD, _CYAN)} #{seq} {method} {path}",
                file=sys.stderr,
            )
            print(json.dumps(req_body, indent=2, ensure_ascii=False)[:8000], file=sys.stderr)
            sys.stderr.flush()

        # Build upstream request headers
        fwd_headers: dict[str, str] = {}
        for k, v in request.headers.items():
            kl = k.lower()
            if kl == "host":
                continue  # will be set by aiohttp from URL
            if kl == "accept-encoding":
                continue  # prevent gzip to keep SSE parseable
            fwd_headers[k] = v

        upstream_url = upstream.rstrip("/") + path

        t0 = time.monotonic()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method,
                    upstream_url,
                    headers=fwd_headers,
                    data=raw_body if raw_body else None,
                    timeout=aiohttp.ClientTimeout(total=600),
                ) as upstream_resp:
                    elapsed_ms = int((time.monotonic() - t0) * 1000)

                    # Prepare client response
                    resp = web.StreamResponse(status=upstream_resp.status)
                    for k, v in upstream_resp.headers.items():
                        if k.lower() not in _HOP_BY_HOP:
                            resp.headers[k] = v
                    await resp.prepare(request)

                    # Stream body back, accumulating SSE events
                    acc = SSEAccumulator()
                    async for chunk in upstream_resp.content.iter_any():
                        acc.feed(chunk)
                        await resp.write(chunk)
                    acc.flush()
                    await resp.write_eof()

                    elapsed_ms = int((time.monotonic() - t0) * 1000)

                    if verbose and acc.usage:
                        print(
                            f"\n{_c('<<< RESPONSE', _BOLD, _GREEN)} #{seq} "
                            f"usage={json.dumps(acc.usage)}",
                            file=sys.stderr,
                        )
                        sys.stderr.flush()

                    # Console summary
                    if is_json:
                        print_summary(
                            seq=seq,
                            method=method,
                            path=path.split("?")[0],
                            status=upstream_resp.status,
                            elapsed_ms=elapsed_ms,
                            model=acc.model or req_body.get("model") if req_body else None,
                            breakpoints=breakpoints,
                            usage=acc.usage,
                        )
                    else:
                        # Non-JSON endpoint: simple one-liner
                        print(
                            f"  {_c(f'#{seq}', _DIM)} {method} {path} → "
                            f"{upstream_resp.status} ({elapsed_ms}ms)",
                            file=sys.stderr,
                        )
                        sys.stderr.flush()

                    # JSONL log
                    logger.log(
                        method=method,
                        path=path,
                        req_headers=req_headers,
                        req_body=req_body,
                        cache_breakpoints=breakpoints,
                        status=upstream_resp.status,
                        resp_headers=dict(upstream_resp.headers),
                        usage=acc.usage,
                        sse_event_count=acc.event_count,
                        elapsed_ms=elapsed_ms,
                    )

                    return resp

        except aiohttp.ClientError as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            print(
                f"  {_c('ERROR', _RED, _BOLD)} #{seq} upstream connection failed: {exc}",
                file=sys.stderr,
            )
            sys.stderr.flush()
            error_body = json.dumps({
                "type": "error",
                "error": {
                    "type": "proxy_error",
                    "message": f"Upstream connection failed: {exc}",
                },
            })
            return web.Response(
                status=502,
                text=error_body,
                content_type="application/json",
            )

    return handle_request


# ─── App bootstrap ─────────────────────────────────────────────────────────

def create_app(upstream: str, log_file: str, verbose: bool) -> web.Application:
    logger = JsonlLogger(log_file)
    handler = _make_handler(upstream, logger, verbose)
    app = web.Application()
    # Catch-all route: forward everything
    app.router.add_route("*", "/{path_info:.*}", handler)
    return app


def main() -> None:
    args = parse_args()

    print(
        f"\n{_c('Claude Code Cache Debug Proxy', _BOLD, _CYAN)}",
        file=sys.stderr,
    )
    print(f"  Listen:   http://localhost:{args.port}", file=sys.stderr)
    print(f"  Upstream: {args.upstream}", file=sys.stderr)
    print(f"  Log file: {args.log_file}", file=sys.stderr)
    print(f"  Verbose:  {args.verbose}", file=sys.stderr)
    print(file=sys.stderr)
    print(
        f"  {_c('Usage:', _BOLD)} ANTHROPIC_BASE_URL=http://localhost:{args.port} claude",
        file=sys.stderr,
    )
    print(file=sys.stderr)
    sys.stderr.flush()

    app = create_app(args.upstream, args.log_file, args.verbose)
    web.run_app(app, host="0.0.0.0", port=args.port, print=lambda msg: print(f"  {msg}", file=sys.stderr))


if __name__ == "__main__":
    main()
