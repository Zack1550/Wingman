#!/usr/bin/env python3
"""Run one mission.

    python -m harness.run "take off to 15 m and hover"
    python -m harness.run --provider anthropic "fly copter_1 50 m north at 20 m"
    python -m harness.run --max-steps 4 "what is copter_1 doing?"

Needs the gateway running, and SITL behind it.
"""
import argparse
import sys

from harness.loop import Budget, run
from harness.providers import build_provider
from harness.toolbox import ToolBox
from harness.trace import Trace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task")
    parser.add_argument("--provider", default="ollama",
                        choices=["ollama", "anthropic"])
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--max-tool-calls", type=int, default=30)
    parser.add_argument("--deadline", type=float, default=300.0)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    provider = build_provider(args.provider, model=args.model)
    budget = Budget(max_model_steps=args.max_steps,
                    max_tool_calls=args.max_tool_calls,
                    deadline_s=args.deadline)

    with ToolBox() as toolbox:
        print(f"tools: {', '.join(toolbox.names())}", file=sys.stderr)
        print(f"model: {provider.describe()}\n", file=sys.stderr)
        with Trace(task=args.task, provider=provider.describe()) as trace:
            result = run(args.task, provider, toolbox, budget, trace)
            trace.close(stop_reason=result.stop_reason,
                        final_text=result.final_text)

    if not args.quiet:
        for message in result.messages:
            if message["role"] == "assistant":
                for call in message.get("tool_calls", []):
                    print(f"  → {call.name}({_short(call.arguments)})")
            elif message["role"] == "tool":
                print(f"    {_short(message['content'], 150)}")

    print(f"\nstop_reason : {result.stop_reason}")
    print(f"steps       : {result.steps}   tool calls: {result.tool_calls}")
    print(f"trace       : {result.trace_path}")
    print(f"\n{result.final_text}")
    return 0 if result.completed else 1


def _short(value, limit=90):
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[:limit] + "…"


if __name__ == "__main__":
    raise SystemExit(main())
