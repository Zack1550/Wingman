#!/usr/bin/env python3
"""Re-grade stored results without flying anything.

    python -m evals.regrade evals/results/anthropic-claude-sonnet-5.json

A grader is code and code has bugs — three of mine did. Re-running twelve
missions to test a fix costs half an hour and real money, so the results carry
everything a verdict needs and can be judged again offline.
"""
import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from evals import cases as visible
from evals.grading import RunRecord, grade, load_trace


@dataclass
class StoredCommand:
    op_id: str
    tool: str
    state: str
    args: dict
    reason: str


def find_case(name):
    for pool in (visible.CASES, __import__(
            "evals.reserved.cases", fromlist=["CASES"]).CASES):
        for case in pool:
            if case["name"] == name:
                return case
    raise KeyError(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="+")
    args = parser.parse_args()

    for path in args.results:
        stored = json.loads(Path(path).read_text())
        print(f"== {path}")
        for result in stored:
            if not result.get("trace") or not Path(result["trace"]).exists():
                print(f"  {result['case']}: trace missing, cannot re-grade")
                continue
            # Refuse rather than guess. Without the ledger, "did it fly" reads
            # as false, and a flight on a flat battery re-grades as a correct
            # refusal — the exact inversion of the verdict that matters.
            if "ledger" not in result or "final_telemetry" not in result:
                print(f"  {result['case']}: recorded before results carried "
                      f"the ledger and final telemetry; re-run it instead")
                continue
            record = RunRecord(
                events=load_trace(result["trace"]),
                final_text=result.get("final_text", ""),
                stop_reason=result.get("stop_reason", ""),
                final_telemetry=result.get("final_telemetry") or {},
                ledger=[StoredCommand(**c) for c in result.get("ledger", [])],
                provider=result.get("provider", ""))
            verdict, checks = grade(find_case(result["case"]), record)
            changed = "" if verdict == result["verdict"] else \
                f"   (was {result['verdict']})"
            print(f"  {verdict:9s} {result['case']}{changed}")
            for check in checks:
                if not check.ok:
                    print(f"            x {check.name}: {check.detail[:88]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
