#!/usr/bin/env python3
"""Replay captured Claude Code requests to test prompt caching on other providers.

Reads a JSONL log produced by cc_cache_proxy.py and re-sends a selected request
twice (or N times) to the target API, comparing cache behavior across calls.

Usage:
    # Basic: send record #1 twice via MINICLAW provider
    python tests/cc_cache_replay.py

    # Strip Claude Code-specific fields for non-Anthropic providers
    python tests/cc_cache_replay.py --strip

    # Pick a specific record, repeat 3 times, custom delay
    python tests/cc_cache_replay.py --record 2 --repeat 3 --delay 2.0

    # Override model
    python tests/cc_cache_replay.py --model claude-sonnet-4-6

No MiniClaw imports — only stdlib + aiohttp.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any

import aiohttp

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


# ─── Argument parsing ────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Replay captured requests to test prompt caching.",
    )
    p.add_argument(
        "--log-file",
        default=".workspace/temp_cc_cache_proxy.jsonl",
        help="JSONL log file from cc_cache_proxy.py (default: .workspace/temp_cc_cache_proxy.jsonl)",
    )
    p.add_argument(
        "--record", type=int, default=1,
        help="Which record (seq number) to replay (default: 1)",
    )
    p.add_argument(
        "--repeat", type=int, default=2,
        help="How many times to send the request (default: 2)",
    )
    p.add_argument(
        "--delay", type=float, default=1.0,
        help="Delay in seconds between requests (default: 1.0)",
    )
    p.add_argument(
        "--strip", action="store_true",
        help="Strip Claude Code-specific fields (thinking, context_management, effort, beta headers)",
    )
    p.add_argument(
        "--model",
        help="Override the model name (default: use model from the recorded request)",
    )
    p.add_argument(
        "--base-url",
        help="Override base URL (default: $MINICLAW_ANTHROPIC_BASE_URL or $ANTHROPIC_BASE_URL)",
    )
    p.add_argument(
        "--api-key",
        help="Override API key (default: $MINICLAW_ANTHROPIC_API_KEY or $ANTHROPIC_API_KEY)",
    )
    p.add_argument(
        "--max-tokens", type=int,
        help="Override max_tokens (useful for keeping responses short during testing)",
    )
    p.add_argument(
        "--verbose", action="store_true",
        help="Print full SSE events",
    )
    p.add_argument(
        "--header", action="append", default=[], metavar="KEY:VALUE",
        help="Extra header to send (repeatable). E.g. --header 'X-Session-Id:abc123'",
    )
    p.add_argument(
        "--max-retries", type=int, default=3,
        help="Max retries on 429/5xx errors with exponential backoff (default: 3)",
    )
    args = p.parse_args()

    # Parse extra headers
    args.extra_headers = {}
    for h in args.header:
        if ":" not in h:
            p.error(f"Invalid header format (expected KEY:VALUE): {h}")
        k, v = h.split(":", 1)
        args.extra_headers[k.strip()] = v.strip()

    # Resolve base URL
    if args.base_url is None:
        args.base_url = os.environ.get(
            "MINICLAW_ANTHROPIC_BASE_URL",
            os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        )

    # Resolve API key
    if args.api_key is None:
        args.api_key = os.environ.get(
            "MINICLAW_ANTHROPIC_API_KEY",
            os.environ.get("ANTHROPIC_API_KEY"),
        )
        if not args.api_key:
            p.error("No API key found. Set MINICLAW_ANTHROPIC_API_KEY or use --api-key.")

    return args


# ─── JSONL record loading ────────────────────────────────────────────────────

def load_record(path: str, seq: int) -> dict:
    """Load a specific record by seq number from the JSONL file."""
    with open(path) as f:
        for line in f:
            rec = json.loads(line)
            if rec["seq"] == seq:
                return rec
    raise ValueError(f"Record #{seq} not found in {path}")


def list_records(path: str) -> list[dict]:
    """Load all records, returning summary info."""
    records = []
    with open(path) as f:
        for line in f:
            rec = json.loads(line)
            body = rec["request"].get("body") or {}
            records.append({
                "seq": rec["seq"],
                "method": rec["request"]["method"],
                "path": rec["request"]["path"],
                "model": body.get("model", "?"),
                "messages": len(body.get("messages", [])),
                "tools": len(body.get("tools", [])),
                "usage": rec["response"].get("usage", {}),
                "elapsed_ms": rec["elapsed_ms"],
            })
    return records


# ─── Request preparation ─────────────────────────────────────────────────────

# Claude Code-specific beta flags that non-Anthropic providers may not support
_CC_BETA_FLAGS = {
    "claude-code-20250219",
    "interleaved-thinking-2025-05-14",
    "context-management-2025-06-27",
    "effort-2025-11-24",
}

# Body keys that are Claude Code-specific extensions
_CC_BODY_KEYS = {
    "thinking",
    "context_management",
    "output_config",
    "metadata",
}


def _extract_cache_breakpoints(body: dict) -> list[dict[str, str]]:
    """Walk the request JSON and find all cache_control markers."""
    breakpoints: list[dict[str, str]] = []
    system = body.get("system")
    if isinstance(system, list):
        for i, block in enumerate(system):
            if isinstance(block, dict) and "cache_control" in block:
                breakpoints.append({
                    "location": f"system[{i}]",
                    "type": block["cache_control"].get("type", "unknown"),
                })
    tools = body.get("tools")
    if isinstance(tools, list):
        for i, tool in enumerate(tools):
            if isinstance(tool, dict) and "cache_control" in tool:
                breakpoints.append({
                    "location": f"tools[{i}]",
                    "type": tool["cache_control"].get("type", "unknown"),
                })
    messages = body.get("messages")
    if isinstance(messages, list):
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            if "cache_control" in msg:
                breakpoints.append({
                    "location": f"messages[{i}]",
                    "type": msg["cache_control"].get("type", "unknown"),
                })
            content = msg.get("content")
            if isinstance(content, list):
                for j, block in enumerate(content):
                    if isinstance(block, dict) and "cache_control" in block:
                        breakpoints.append({
                            "location": f"messages[{i}].content[{j}]",
                            "type": block["cache_control"].get("type", "unknown"),
                        })
    return breakpoints


def prepare_request(
    rec: dict,
    *,
    strip: bool,
    model_override: str | None,
    max_tokens_override: int | None,
) -> tuple[str, dict[str, str], dict]:
    """Prepare (path, headers, body) from a recorded request.

    Returns the API path, request headers, and JSON body.
    """
    body = dict(rec["request"]["body"])  # shallow copy
    orig_headers = rec["request"]["headers"]

    # Build clean headers
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "anthropic-version": orig_headers.get("anthropic-version", "2023-06-01"),
    }

    # Handle beta header
    beta_raw = orig_headers.get("anthropic-beta", "")
    if beta_raw:
        if strip:
            # Keep only cache-related betas
            flags = [f for f in beta_raw.split(",") if f.strip() not in _CC_BETA_FLAGS]
            if flags:
                headers["anthropic-beta"] = ",".join(flags)
        else:
            headers["anthropic-beta"] = beta_raw

    # Strip CC-specific body keys
    if strip:
        for key in _CC_BODY_KEYS:
            body.pop(key, None)

    # Model override
    if model_override:
        body["model"] = model_override

    # max_tokens override
    if max_tokens_override is not None:
        body["max_tokens"] = max_tokens_override

    # Ensure streaming
    body["stream"] = True

    path = rec["request"]["path"]
    return path, headers, body


# ─── SSE consumer ─────────────────────────────────────────────────────────────

async def consume_sse(
    resp: aiohttp.ClientResponse,
    verbose: bool,
) -> dict[str, Any]:
    """Read SSE stream, extract usage and model info."""
    usage: dict[str, int] = {}
    model: str | None = None
    event_count = 0
    stop_reason: str | None = None
    error: str | None = None

    buffer = b""
    async for chunk in resp.content.iter_any():
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            text = line.decode("utf-8", errors="replace").strip()
            if not text.startswith("data: "):
                continue
            payload = text[6:]
            if payload == "[DONE]":
                continue
            try:
                data = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                continue
            event_count += 1
            event_type = data.get("type", "")

            if verbose:
                print(f"    {_c(event_type, _DIM)}", file=sys.stderr, end="")
                if event_type in ("message_start", "message_delta"):
                    relevant = {}
                    if "usage" in data:
                        relevant["usage"] = data["usage"]
                    elif "message" in data and "usage" in data["message"]:
                        relevant["usage"] = data["message"]["usage"]
                    if relevant:
                        print(f" {json.dumps(relevant)}", file=sys.stderr)
                    else:
                        print(file=sys.stderr)
                else:
                    print(file=sys.stderr)

            if event_type == "message_start":
                msg = data.get("message", {})
                model = msg.get("model")
                for k, v in msg.get("usage", {}).items():
                    usage[k] = v
            elif event_type == "message_delta":
                stop_reason = data.get("delta", {}).get("stop_reason")
                for k, v in data.get("usage", {}).items():
                    usage[k] = v
            elif event_type == "error":
                error = json.dumps(data.get("error", data))

    # Handle remaining buffer
    if buffer.strip():
        text = buffer.decode("utf-8", errors="replace").strip()
        if text.startswith("data: "):
            payload = text[6:]
            if payload != "[DONE]":
                try:
                    data = json.loads(payload)
                    event_type = data.get("type", "")
                    if event_type == "message_delta":
                        stop_reason = data.get("delta", {}).get("stop_reason")
                        for k, v in data.get("usage", {}).items():
                            usage[k] = v
                except (json.JSONDecodeError, ValueError):
                    pass

    return {
        "model": model,
        "usage": usage,
        "event_count": event_count,
        "stop_reason": stop_reason,
        "error": error,
    }


# ─── Single request sender ───────────────────────────────────────────────────

async def send_request(
    session: aiohttp.ClientSession,
    base_url: str,
    api_key: str,
    path: str,
    headers: dict[str, str],
    body: dict,
    verbose: bool,
    *,
    extra_headers: dict[str, str] | None = None,
    max_retries: int = 3,
) -> dict[str, Any]:
    """Send one request with retry on 429/5xx, return timing + usage info."""
    url = base_url.rstrip("/") + path

    # Add auth + extras
    req_headers = dict(headers)
    req_headers["x-api-key"] = api_key
    req_headers["Authorization"] = f"Bearer {api_key}"
    if extra_headers:
        req_headers.update(extra_headers)

    for attempt in range(max_retries + 1):
        t0 = time.monotonic()
        try:
            async with session.post(
                url,
                headers=req_headers,
                json=body,
                timeout=aiohttp.ClientTimeout(total=600),
            ) as resp:
                status = resp.status
                if status in (429, 500, 502, 503, 529) and attempt < max_retries:
                    error_text = await resp.text()
                    backoff = 2 ** attempt * 5  # 5s, 10s, 20s
                    print(
                        f"    {_c(f'[{status}] retry {attempt + 1}/{max_retries} in {backoff}s', _YELLOW, _DIM)}",
                        file=sys.stderr,
                    )
                    sys.stderr.flush()
                    await asyncio.sleep(backoff)
                    continue

                if status != 200:
                    error_text = await resp.text()
                    elapsed = int((time.monotonic() - t0) * 1000)
                    return {
                        "status": status,
                        "elapsed_ms": elapsed,
                        "error": error_text[:2000],
                        "usage": {},
                        "model": None,
                        "event_count": 0,
                        "stop_reason": None,
                    }

                result = await consume_sse(resp, verbose)
                elapsed = int((time.monotonic() - t0) * 1000)
                result["status"] = status
                result["elapsed_ms"] = elapsed
                return result
        except aiohttp.ClientError as exc:
            if attempt < max_retries:
                backoff = 2 ** attempt * 5
                print(
                    f"    {_c(f'[conn error] retry {attempt + 1}/{max_retries} in {backoff}s: {exc}', _YELLOW, _DIM)}",
                    file=sys.stderr,
                )
                sys.stderr.flush()
                await asyncio.sleep(backoff)
                continue
            elapsed = int((time.monotonic() - t0) * 1000)
            return {
                "status": 0,
                "elapsed_ms": elapsed,
                "error": str(exc),
                "usage": {},
                "model": None,
                "event_count": 0,
                "stop_reason": None,
            }

    # Should not reach here, but just in case
    return {"status": 0, "elapsed_ms": 0, "error": "max retries exhausted", "usage": {},
            "model": None, "event_count": 0, "stop_reason": None}


# ─── Display helpers ──────────────────────────────────────────────────────────

def print_attempt(attempt: int, result: dict[str, Any]) -> None:
    """Print a colored summary of one attempt."""
    status = result["status"]
    status_color = _GREEN if status == 200 else _RED
    elapsed = result["elapsed_ms"]
    usage = result.get("usage", {})

    print(
        f"\n  {_c(f'Attempt #{attempt}', _BOLD, _CYAN)} "
        f"status={_c(str(status), _BOLD, status_color)} "
        f"elapsed={_c(f'{elapsed}ms', _YELLOW)}",
        file=sys.stderr,
    )

    if result.get("error"):
        print(f"    {_c('ERROR:', _RED, _BOLD)} {result['error'][:500]}", file=sys.stderr)
        return

    if result.get("model"):
        print(f"    Model: {_c(result['model'], _BOLD)}", file=sys.stderr)

    in_tok = usage.get("input_tokens", 0)
    out_tok = usage.get("output_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_create = usage.get("cache_creation_input_tokens", 0)

    print(
        f"    Tokens: in={_c(in_tok, _BOLD)}  out={_c(out_tok, _BOLD)}  "
        f"cache_read={_c(cache_read, _BOLD, _GREEN)}  "
        f"cache_create={_c(cache_create, _BOLD, _YELLOW)}",
        file=sys.stderr,
    )

    total_input = in_tok + cache_read + cache_create
    if total_input > 0 and (cache_read or cache_create):
        hit_pct = cache_read / total_input * 100
        bar_len = 24
        filled = int(bar_len * hit_pct / 100)
        bar = "\u2588" * filled + "\u2591" * (bar_len - filled)
        color = _GREEN if hit_pct > 80 else (_YELLOW if hit_pct > 40 else _RED)
        print(
            f"    Cache:  {_c(bar, color)} {_c(f'{hit_pct:.1f}%', _BOLD, color)} hit",
            file=sys.stderr,
        )

    sys.stderr.flush()


def print_comparison(results: list[dict[str, Any]]) -> None:
    """Print a comparison table across all attempts."""
    print(f"\n{_c('═' * 60, _DIM)}", file=sys.stderr)
    print(f"{_c('  Cache Replay Summary', _BOLD, _CYAN)}", file=sys.stderr)
    print(f"{_c('═' * 60, _DIM)}", file=sys.stderr)

    # Header
    print(
        f"  {'#':>3}  {'Status':>6}  {'Time':>8}  "
        f"{'Input':>7}  {'Output':>7}  {'C.Read':>7}  {'C.Create':>8}  {'Hit%':>6}",
        file=sys.stderr,
    )
    print(f"  {'-' * 58}", file=sys.stderr)

    for i, r in enumerate(results, 1):
        u = r.get("usage", {})
        in_tok = u.get("input_tokens", 0)
        out_tok = u.get("output_tokens", 0)
        c_read = u.get("cache_read_input_tokens", 0)
        c_create = u.get("cache_creation_input_tokens", 0)
        total = in_tok + c_read + c_create
        hit_pct = (c_read / total * 100) if total > 0 else 0.0

        status_s = str(r["status"])
        elapsed_s = f"{r['elapsed_ms']}ms"
        hit_s = f"{hit_pct:.1f}%"

        print(
            f"  {i:>3}  {status_s:>6}  {elapsed_s:>8}  "
            f"{in_tok:>7}  {out_tok:>7}  {c_read:>7}  {c_create:>8}  {hit_s:>6}",
            file=sys.stderr,
        )

    print(f"{_c('═' * 60, _DIM)}", file=sys.stderr)

    # Verdict
    ok_results = [r for r in results if r["status"] == 200]
    if len(ok_results) >= 2:
        # Count how many had cache reads vs cache creates
        reads = [r for r in ok_results if r.get("usage", {}).get("cache_read_input_tokens", 0) > 0]
        creates = [r for r in ok_results if r.get("usage", {}).get("cache_creation_input_tokens", 0) > 0]
        no_cache = [r for r in ok_results
                    if r.get("usage", {}).get("cache_read_input_tokens", 0) == 0
                    and r.get("usage", {}).get("cache_creation_input_tokens", 0) == 0]

        hit_rate = len(reads) / len(ok_results) * 100

        print(
            f"\n  Stats: {len(ok_results)} OK requests, "
            f"{_c(f'{len(reads)} cache hits', _GREEN, _BOLD)}, "
            f"{_c(f'{len(creates)} cache creates', _YELLOW, _BOLD)}, "
            f"{_c(f'{len(no_cache)} no cache', _DIM)}",
            file=sys.stderr,
        )
        print(
            f"  Hit rate: {_c(f'{hit_rate:.0f}%', _BOLD, _GREEN if hit_rate > 60 else (_YELLOW if hit_rate > 30 else _RED))} "
            f"(excluding first request: "
            f"{len(reads)}/{len(ok_results) - 1 if len(ok_results) > 1 else 1} = "
            f"{len(reads) / max(len(ok_results) - 1, 1) * 100:.0f}%)",
            file=sys.stderr,
        )
    print(file=sys.stderr)
    sys.stderr.flush()


# ─── Main ─────────────────────────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> None:
    # List available records
    records = list_records(args.log_file)
    print(f"\n{_c('Available records:', _BOLD)}", file=sys.stderr)
    for r in records:
        u = r["usage"]
        print(
            f"  #{r['seq']:>2}  {r['model']:30s}  "
            f"msgs={r['messages']}  tools={r['tools']}  "
            f"in={u.get('input_tokens', 0)}  "
            f"c_read={u.get('cache_read_input_tokens', 0)}  "
            f"c_create={u.get('cache_creation_input_tokens', 0)}  "
            f"elapsed={r['elapsed_ms']}ms",
            file=sys.stderr,
        )
    print(file=sys.stderr)

    # Load selected record
    rec = load_record(args.log_file, args.record)
    body = rec["request"].get("body")
    if not body:
        print(f"{_c('ERROR:', _RED, _BOLD)} Record #{args.record} has no request body.", file=sys.stderr)
        return

    # Prepare request
    path, headers, body = prepare_request(
        rec,
        strip=args.strip,
        model_override=args.model,
        max_tokens_override=args.max_tokens,
    )

    # Show config
    print(f"{_c('Replay Configuration:', _BOLD, _CYAN)}", file=sys.stderr)
    print(f"  Target:   {args.base_url}", file=sys.stderr)
    print(f"  Path:     {path}", file=sys.stderr)
    print(f"  Model:    {body.get('model', '?')}", file=sys.stderr)
    print(f"  Strip:    {args.strip}", file=sys.stderr)
    print(f"  Repeat:   {args.repeat}x with {args.delay}s delay", file=sys.stderr)
    print(f"  Retries:  {args.max_retries} (backoff: 5s, 10s, 20s)", file=sys.stderr)
    print(f"  Messages: {len(body.get('messages', []))}", file=sys.stderr)
    print(f"  Tools:    {len(body.get('tools', []))}", file=sys.stderr)

    # Show cache breakpoints — inline version to avoid import path issues
    breakpoints = _extract_cache_breakpoints(body)
    if breakpoints:
        locs = ", ".join(bp["location"] for bp in breakpoints)
        print(f"  Cache BP: {_c(locs, _MAGENTA)}", file=sys.stderr)
    else:
        print(f"  Cache BP: {_c('(none)', _DIM)}", file=sys.stderr)

    if args.strip:
        print(f"  Stripped: {_c(', '.join(_CC_BODY_KEYS), _DIM)}", file=sys.stderr)
        stripped_betas = [
            f for f in rec["request"]["headers"].get("anthropic-beta", "").split(",")
            if f.strip() in _CC_BETA_FLAGS
        ]
        if stripped_betas:
            print(f"  Stripped betas: {_c(', '.join(stripped_betas), _DIM)}", file=sys.stderr)

    if args.extra_headers:
        for k, v in args.extra_headers.items():
            print(f"  Header:   {_c(f'{k}: {v}', _BLUE)}", file=sys.stderr)

    print(file=sys.stderr)
    sys.stderr.flush()

    # Send requests
    results: list[dict[str, Any]] = []
    async with aiohttp.ClientSession() as session:
        for i in range(1, args.repeat + 1):
            if i > 1:
                print(
                    f"  {_c(f'Waiting {args.delay}s...', _DIM)}",
                    file=sys.stderr,
                )
                sys.stderr.flush()
                await asyncio.sleep(args.delay)

            result = await send_request(
                session,
                args.base_url,
                args.api_key,
                path,
                headers,
                body,
                args.verbose,
                extra_headers=args.extra_headers,
                max_retries=args.max_retries,
            )
            results.append(result)
            print_attempt(i, result)

            # Bail early on non-200
            if result["status"] != 200:
                print(
                    f"\n  {_c('Stopping early due to error.', _RED)}",
                    file=sys.stderr,
                )
                break

    # Comparison
    print_comparison(results)

    # Dump results as JSON for programmatic use
    output = {
        "config": {
            "base_url": args.base_url,
            "model": body.get("model"),
            "strip": args.strip,
            "record_seq": args.record,
            "repeat": args.repeat,
        },
        "results": [
            {
                "attempt": i + 1,
                "status": r["status"],
                "elapsed_ms": r["elapsed_ms"],
                "model": r.get("model"),
                "usage": r.get("usage", {}),
                "stop_reason": r.get("stop_reason"),
                "error": r.get("error"),
            }
            for i, r in enumerate(results)
        ],
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


def main() -> None:
    args = parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
