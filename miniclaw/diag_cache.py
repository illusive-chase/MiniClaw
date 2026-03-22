"""Prompt-cache diagnostic: verify Anthropic caching works end-to-end.

Sends three sequential requests — the first two with cache_control markers,
the third without — and reports whether cached tokens were created and read.

Uses miniclaw's config.yaml for credentials instead of raw env vars.
"""

import argparse
import sys
import time

from anthropic import Anthropic

from miniclaw.config import load_config

# A long-ish system prompt so it crosses the minimum caching threshold
# (1024 tokens for sonnet).
SYSTEM_PROMPT = (
    "You are a very helpful coding assistant. " * 1000
    + "\nAlways respond concisely."
)

TOOLS = [
    {
        "name": "get_weather",
        "description": (
            "Get current weather for a given location. "
            "Returns temperature, conditions, humidity, and wind speed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City and state/country",
                },
                "unit": {
                    "type": "string",
                    "enum": ["celsius", "fahrenheit"],
                    "description": "Temperature unit",
                },
            },
            "required": ["location"],
        },
    },
]

MESSAGES = [
    {"role": "user", "content": "What is 2+2? Reply in one word."},
]


def build_request(model: str, *, cache: bool) -> dict:
    """Build the messages.create kwargs, optionally adding cache_control markers."""
    system = [{"type": "text", "text": SYSTEM_PROMPT}]
    tools = [dict(t) for t in TOOLS]  # shallow copy
    msgs = [dict(m) for m in MESSAGES]

    if cache:
        # Breakpoint 1: system prompt
        system[-1]["cache_control"] = {"type": "ephemeral"}
        # Breakpoint 2: tools
        tools[-1]["cache_control"] = {"type": "ephemeral"}
        # Breakpoint 3: last user message
        msgs[-1] = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": msgs[-1]["content"],
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }

    return {
        "model": model,
        "max_tokens": 8192,
        "system": system,
        "tools": tools,
        "messages": msgs,
    }


def print_usage(label: str, usage) -> None:
    print(f"\n{'=' * 50}")
    print(f"  {label}")
    print(f"{'=' * 50}")
    print(f"  input_tokens:                {usage.input_tokens}")
    print(f"  output_tokens:               {usage.output_tokens}")
    print(f"  cache_creation_input_tokens: {getattr(usage, 'cache_creation_input_tokens', 0) or 0}")
    print(f"  cache_read_input_tokens:     {getattr(usage, 'cache_read_input_tokens', 0) or 0}")
    print()


def _response_text(response) -> str:
    if response.content and response.content[0].type == "text":
        return response.content[0].text
    return "(tool_use)"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Diagnose Anthropic prompt caching behaviour.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the model from config.yaml",
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=0,
        help="Seconds to wait between calls for cache propagation (default: 0)",
    )
    args = parser.parse_args()

    cfg = load_config()
    provider = cfg["provider"]

    if provider["type"] != "anthropic":
        print(f"ERROR: provider.type is '{provider['type']}', expected 'anthropic'")
        sys.exit(1)

    api_key = provider.get("api_key", "")
    base_url = provider.get("base_url", "")
    if not api_key:
        print("ERROR: provider.api_key is empty in config.yaml")
        sys.exit(1)
    if not base_url:
        print("ERROR: provider.base_url is empty in config.yaml")
        sys.exit(1)

    model = args.model or provider.get("model", "claude-opus-4-6")

    client = Anthropic(api_key=api_key, base_url=base_url)
    print(f"Model: {model}")

    # ── Call 1: should create cache ──
    print("\n>>> Call 1 (expect cache CREATION) ...")
    kwargs = build_request(model, cache=True)
    t0 = time.monotonic()
    r1 = client.messages.create(**kwargs)
    dt1 = time.monotonic() - t0
    print_usage(f"Call 1  ({dt1:.2f}s)", r1.usage)
    print(f"  Response: {_response_text(r1)}")

    print(f"\n... waiting {args.delay}s for cache propagation ...")
    time.sleep(args.delay)

    # ── Call 2: should read from cache ──
    print("\n>>> Call 2 (expect cache READ) ...")
    kwargs = build_request(model, cache=True)
    t0 = time.monotonic()
    r2 = client.messages.create(**kwargs)
    dt2 = time.monotonic() - t0
    print_usage(f"Call 2  ({dt2:.2f}s)", r2.usage)
    print(f"  Response: {_response_text(r2)}")

    print(f"\n... waiting {args.delay}s for cache propagation ...")
    time.sleep(args.delay)

    # ── Call 3 (no cache markers): baseline comparison ──
    print("\n>>> Call 3 (NO cache markers, baseline) ...")
    kwargs = build_request(model, cache=False)
    t0 = time.monotonic()
    r3 = client.messages.create(**kwargs)
    dt3 = time.monotonic() - t0
    print_usage(f"Call 3  ({dt3:.2f}s, no cache)", r3.usage)
    print(f"  Response: {_response_text(r3)}")

    # ── Summary ──
    c1_create = getattr(r1.usage, "cache_creation_input_tokens", 0) or 0
    c2_read = getattr(r2.usage, "cache_read_input_tokens", 0) or 0
    print("\n" + "=" * 50)
    print("  SUMMARY")
    print("=" * 50)
    if c1_create > 0 and c2_read > 0:
        print("  Prompt caching is WORKING.")
        print(f"  Call 1 created {c1_create} cached tokens.")
        print(f"  Call 2 read    {c2_read} cached tokens.")
        print(f"  Latency: {dt1:.2f}s -> {dt2:.2f}s (delta {dt1 - dt2:+.2f}s)")
    elif c1_create > 0 and c2_read == 0:
        print("  Cache was CREATED on call 1 but NOT READ on call 2.")
        print("  Possible causes: request body differs, TTL expired, or API-side issue.")
    else:
        print("  No cache activity detected at all.")
        print("  Check: model supports caching, system prompt meets min token threshold.")
    print()


if __name__ == "__main__":
    main()
