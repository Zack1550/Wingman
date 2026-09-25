#!/usr/bin/env python3
"""Plan a mission, approve it command by command, then run it.

    python -m harness.mission "take off to 15 m and hover"
    python -m harness.mission --provider anthropic "fly copter_1 north 50 m"
    python -m harness.mission --approve-all "..."     # demos and tests only
    python -m harness.mission --plan-only "..."       # see the plan, fly nothing

Three separate things happen here, and keeping them separate is the point:

    the model  proposes   a plan, as data
    a human    approves   one exact command at a time
    the host   executes   only what was approved, and only while it still holds

Needs the gateway running, and SITL behind it.
"""
import argparse
import sys
import uuid

from harness.approval import AutoApprovalGate, ConsoleApprovalGate
from harness.executor import Executor, reconcile_outstanding
from harness.planner import describe_situation, make_plan
from harness.providers import build_provider
from harness.store import MissionStore
from harness.toolbox import ToolBox
from harness.trace import Trace
from vehicle_gateway.client import GatewayClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task")
    parser.add_argument("--provider", default="ollama",
                        choices=["ollama", "anthropic"])
    parser.add_argument("--model", default=None)
    parser.add_argument("--db", default=None, help="mission store path")
    parser.add_argument("--approval-ttl", type=float, default=120.0)
    parser.add_argument("--telemetry-port", type=int, default=14552,
                        help="the executor's own telemetry port; the MCP tool "
                             "server keeps 14551, because two listeners on one "
                             "port starve each other")
    parser.add_argument("--plan-only", action="store_true",
                        help="plan and stop; approve nothing, fly nothing")
    decisions = parser.add_mutually_exclusive_group()
    decisions.add_argument("--approve-all", action="store_true",
                           help="grant every approval without asking — for "
                                "demos and tests, never for a real aircraft")
    decisions.add_argument("--refuse-all", action="store_true",
                           help="refuse every approval; proves a plan moves "
                                "nothing")
    args = parser.parse_args()

    run_id = f"run-{uuid.uuid4().hex[:10]}"
    provider = build_provider(args.provider, model=args.model)

    with MissionStore(args.db) as store, ToolBox() as toolbox:
        client = GatewayClient(telemetry_port=args.telemetry_port).start()
        client.await_first_sample(timeout_s=3.0)

        with Trace(task=args.task, provider=provider.describe()) as trace:
            trace.write("mission_started", run_id=run_id, task=args.task)

            # Before anything new, settle anything old. A command left
            # outstanding by a previous process may already have flown.
            recovered = reconcile_outstanding(store, client, trace)
            if recovered:
                print("recovered from a previous run:", file=sys.stderr)
                for op_id, outcome in recovered:
                    print(f"  {op_id} -> {outcome}", file=sys.stderr)

            situation = describe_situation(client)
            print(f"model: {provider.describe()}\n", file=sys.stderr)
            print(f"the aircraft right now:\n{situation}\n", file=sys.stderr)
            print("planning …\n", file=sys.stderr)
            trace.write("situation", situation=situation)
            plan = make_plan(args.task, provider, toolbox, trace,
                             situation=situation)

            if not plan.is_valid:
                # A planner that declines has usually said why, and its reason
                # is more useful than our schema complaint. Show both.
                if plan.summary:
                    print(f"the planner declined:\n  {plan.summary}\n")
                print("no usable plan:")
                for problem in plan.problems:
                    print(f"  - {problem}")
                return 2

            print(plan.describe())
            print()
            if args.plan_only:
                print("(--plan-only: nothing was approved and nothing flew)")
                return 0

            if args.approve_all:
                gate = AutoApprovalGate(grant=True)
                print("!! --approve-all: every command granted unasked\n",
                      file=sys.stderr)
            elif args.refuse_all:
                gate = AutoApprovalGate(grant=False)
            else:
                gate = ConsoleApprovalGate()

            executor = Executor(store, gate, client=client, toolbox=toolbox,
                                trace=trace, approval_ttl_s=args.approval_ttl)
            result = executor.run(plan, run_id)
            trace.write("mission_finished", run_id=run_id,
                        stop_reason=result.stop_reason)

        print(result.describe())
        print(f"\nrun_id : {run_id}")
        print(f"store  : {store.path}")
        print(f"trace  : {trace.path}")

        # The ledger, not the model's closing sentence, is the record.
        print("\nledger:")
        for record in store.commands_for_run(run_id):
            print(f"  {record.op_id}  {record.tool:18s} {record.state:18s} "
                  f"v{record.result_state_version}  {record.reason[:50]}")

        client.close()
    return 0 if result.completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
