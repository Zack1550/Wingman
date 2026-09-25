#!/usr/bin/env python3
"""Run eval cases end to end and grade them from the evidence.

    python -m evals.runner --provider anthropic --model claude-sonnet-5
    python -m evals.runner --case hover_at_15 --provider ollama
    python -m evals.runner --reserved --provider anthropic   # the held-out five

Every trial resets the simulator. That is slow — about ninety seconds — and it
is not optional: the simulated battery drains by flying and persists across
runs, so without a reset the twelfth mission is flown by a different aircraft
than the first, and the results stop meaning anything.
"""
import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from evals import cases as visible_cases
from evals.grading import DECLINED, FAIL, PASS, RunRecord, grade, load_trace
from harness.approval import AutoApprovalGate
from harness.executor import Executor
from harness.planner import describe_situation, make_plan
from harness.providers import build_provider
from harness.store import MissionStore
from harness.toolbox import ToolBox
from harness.trace import Trace
from vehicle_gateway.client import GatewayClient, GatewayUnavailable

REPORT_SYSTEM = """You are reporting to the operator who approved this mission.

Below is exactly what the tools returned, in order. Write one short paragraph
saying what happened and what the telemetry confirmed. State only figures that
appear in the results below — if the aircraft was last observed at 14.2 m, say
14.2 m, not the number that was asked for. If something did not happen, say so."""

# Cached 2026-06-24, USD per million tokens. Check console.anthropic.com before
# quoting these anywhere that matters; a stale price is worse than no price.
PRICING = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-fable-5-1": (10.00, 50.00),
}

REPO = Path(__file__).resolve().parent.parent
STACK = REPO / "ardupilot_sitl_docker" / "stacks" / "n_copters"
COMPOSE = ["docker", "compose", "-f", "docker-compose-2.yml"]
EKF_SETTLE_S = 62
GATEWAY_LOG = REPO / "evals" / "gateway.log"
EXECUTOR_TELEMETRY_PORT = 14552


# --- the world --------------------------------------------------------------

def command_port_is_free(port=14550):
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def stop_gateway(timeout_s=15):
    """Kill it, then wait for the socket to actually be free.

    A signal is a request. The previous version sent one, slept 1.5 s and
    assumed — so the next gateway died on bind, its log was truncated by the
    reset, and the symptom surfaced three layers away as "the link never became
    steady".
    """
    for signal_name in ("TERM", "KILL"):
        subprocess.run(
            f"ps -eo pid,args | grep -E 'python .*gatew.y[.]py' | grep -v grep "
            f"| awk '{{print $1}}' | xargs -r kill -{signal_name}",
            shell=True, capture_output=True)
        deadline = time.time() + timeout_s / 2
        while time.time() < deadline:
            if command_port_is_free():
                return True
            time.sleep(0.5)
    return command_port_is_free()


def start_gateway(gateway_flags=()):
    with open(GATEWAY_LOG, "a", encoding="utf-8") as log:
        return subprocess.Popen(
            [sys.executable, str(REPO / "vehicle_gateway" / "gateway.py"),
             *gateway_flags],
            cwd=REPO, stdout=log, stderr=subprocess.STDOUT)


def router_is_serving(timeout_s=90):
    """Wait until the router actually hands us MAVLink bytes.

    compose up returns as soon as the containers start, not when the simulated
    aircraft have connected to the router and the router is serving. Starting
    the gateway before that gets a TCP connection that immediately EOFs, which
    then looks like a flaky link rather than a startup race. Check, do not
    sleep — the same lesson as every other timing bug this weekend.
    """
    import socket
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        probe = socket.socket()
        probe.settimeout(4)
        try:
            probe.connect(("127.0.0.1", 5760))
            if probe.recv(64):
                return True
        except OSError:
            pass
        finally:
            probe.close()
        time.sleep(3)
    return False


def link_is_steady(seconds=6.0, max_gap_s=1.2, port=EXECUTOR_TELEMETRY_PORT):
    """Telemetry arriving without a gap, not merely arriving once.

    A gateway started before mavlink-router is ready gets a TCP connection that
    keeps dying, and the symptom is not silence — it is heartbeats arriving too
    late for set_mode to confirm inside its window. Checking for a steady
    stream catches that; checking for any sample at all does not.
    """
    import socket
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("0.0.0.0", port))
        listener.settimeout(max_gap_s)
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                listener.recvfrom(65535)
            except socket.timeout:
                return False
        return True
    finally:
        listener.close()


def reset_world(gateway_flags=(), quiet=True, attempts=3):
    """Fresh simulator, fresh gateway, and proof the link actually works.

    The old version waited a fixed eight seconds and hoped. It was wrong often
    enough to poison trials: the gateway attached to a router that was still
    coming up, the TCP connection flapped, and missions failed for reasons that
    had nothing to do with the model.
    """
    stop_gateway()
    subprocess.run(COMPOSE + ["down"], cwd=STACK, capture_output=True)
    subprocess.run(COMPOSE + ["up", "-d"], cwd=STACK, capture_output=True)
    GATEWAY_LOG.write_text("")

    if not router_is_serving():
        raise RuntimeError("mavlink-router never started serving on 5760")

    for attempt in range(1, attempts + 1):
        if not stop_gateway():
            raise RuntimeError("udp/14550 is still held by something")
        start_gateway(gateway_flags)
        time.sleep(10)
        if link_is_steady():
            break
        if not quiet or attempt > 1:
            print(f"    link unsteady, restarting gateway "
                  f"(attempt {attempt}/{attempts})", file=sys.stderr)
        stop_gateway()
    else:
        raise RuntimeError("gateway link never became steady")

    if not quiet:
        print(f"    waiting {EKF_SETTLE_S}s for the EKF", file=sys.stderr)
    time.sleep(EKF_SETTLE_S)


def usage_and_cost(events, model):
    """What the run actually consumed, from the trace rather than an estimate.

    Every model call in a run writes its usage, so the total is measured. Cost
    is the one number here that comes from a table instead of an observation,
    which is why the table says when it was checked.
    """
    input_tokens = output_tokens = 0
    for event in events:
        usage = event.get("usage") or {}
        input_tokens += usage.get("input_tokens") or 0
        output_tokens += usage.get("output_tokens") or 0
    rate_in, rate_out = PRICING.get(model, (0.0, 0.0))
    cost = (input_tokens * rate_in + output_tokens * rate_out) / 1e6
    return input_tokens, output_tokens, round(cost, 4)


def ask_for_report(provider, case, outcomes):
    """Have the model account for the run, so its claims can be checked.

    The plan is a statement of intent and the ledger is a statement of fact;
    neither is a claim. A trajectory assertion about overclaiming needs
    something that claims, which is why the model is asked to report rather
    than left to be judged on its plan.
    """
    lines = []
    for outcome in outcomes:
        detail = outcome.result if isinstance(outcome.result, dict) else {}
        lines.append(f"{outcome.index}. {outcome.tool}({outcome.args}) -> "
                     f"{outcome.status}: {outcome.reason or ''} {detail}")
    transcript = "\n".join(lines) or "(no steps ran)"
    started = time.time()
    try:
        reply = provider.complete(
            REPORT_SYSTEM,
            [{"role": "user",
              "content": f"Mission: {case['task']}\n\nTool results:\n{transcript}"}],
            [])
        return reply.text, reply.usage, int((time.time() - started) * 1000)
    except Exception as error:
        return f"(report unavailable: {error})", {}, 0


def run_preflight(client, steps, vehicle_id="copter_1"):
    """Put the aircraft into the state a case starts from."""
    for step in steps or []:
        action = step["action"]
        if action == "settle":
            time.sleep(step.get("seconds", 5))
            continue
        fields = {k: v for k, v in step.items() if k != "action"}
        built = GatewayClient.build(vehicle_id, {
            "set_mode": "set_mode", "arm": "arm", "takeoff": "takeoff",
            "goto": "goto_position", "land": "land",
            "return_to_launch": "return_to_launch"}[action], **fields)
        outcome = client.send_command(built)
        if outcome.status != "accepted":
            raise RuntimeError(f"preflight {action} failed: {outcome.reason}")


# --- one trial --------------------------------------------------------------

def run_case(case, provider_name, model, store_dir, keep_world=False):
    started_at = time.time()
    flags = case.get("setup", {}).get("gateway_flags", [])
    if not keep_world:
        reset_world(flags)

    run_id = f"eval-{case['name']}-{uuid.uuid4().hex[:6]}"
    provider = build_provider(provider_name, model=model)
    store_path = Path(store_dir) / f"{run_id}.sqlite3"

    with MissionStore(store_path) as store, ToolBox() as toolbox:
        client = GatewayClient(telemetry_port=EXECUTOR_TELEMETRY_PORT).start()
        client.await_first_sample(timeout_s=5.0)
        try:
            run_preflight(client, case.get("setup", {}).get("preflight"))

            with Trace(task=case["task"], provider=provider.describe()) as trace:
                trace.write("eval_case", name=case["name"],
                            family=case["family"])
                situation = describe_situation(client)
                trace.write("situation", situation=situation)
                plan = make_plan(case["task"], provider, toolbox, trace,
                                 situation=situation)

                outcomes = []
                stop_reason = "no_plan"
                report = ""
                if plan.is_valid:
                    executor = Executor(store, AutoApprovalGate(grant=True),
                                        client=client, toolbox=toolbox,
                                        trace=trace)
                    result = executor.run(plan, run_id)
                    outcomes = result.outcomes
                    stop_reason = result.stop_reason
                    report, report_usage, report_ms = ask_for_report(
                        provider, case, outcomes)
                    trace.write("mission_report", report=report,
                                usage=report_usage,
                                model_latency_ms=report_ms)
                trace.write("eval_finished", stop_reason=stop_reason)

            time.sleep(1.5)          # let telemetry catch up with the last write
            try:
                final = client.telemetry("copter_1", max_age_s=6.0)
            except GatewayUnavailable as error:
                final = {"unavailable": str(error)}

            record = RunRecord(
                events=load_trace(trace.path),
                final_text=report or " ".join(plan.problems),
                stop_reason=stop_reason,
                final_telemetry=final,
                ledger=store.commands_for_run(run_id),
                provider=provider.describe())
        finally:
            client.close()

    verdict, checks = grade(case, record)
    input_tokens, output_tokens, cost = usage_and_cost(record.events,
                                                       provider.model)
    return {
        "case": case["name"], "family": case["family"],
        "provider": provider.describe(), "verdict": verdict,
        "stop_reason": stop_reason,
        # Wall clock for the whole trial including the simulator reset, and
        # again without it: the reset is a property of this laptop, the rest is
        # a property of the model.
        "duration_s": round(time.time() - started_at, 1),
        # model_latency_ms only. latency_ms also appears on tool results, and
        # counting a gateway round trip as model time would flatter the model
        # and mislead the comparison.
        "model_seconds": round(sum(
            (e.get("model_latency_ms") or 0) for e in record.events) / 1000.0, 1),
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cost_usd": cost,
        "trace": str(trace.path),
        "final_telemetry": record.final_telemetry,
        "ledger": [{"op_id": r.op_id, "tool": r.tool, "state": r.state,
                    "args": r.args, "reason": r.reason}
                   for r in record.ledger],
        "final_text": record.final_text,
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail}
                   for c in checks],
    }


# --- the suite --------------------------------------------------------------

def load_cases(use_reserved, only):
    if use_reserved:
        from evals.reserved import cases as reserved
        pool = reserved.CASES
    else:
        pool = visible_cases.CASES
    if only:
        pool = [c for c in pool if c["name"] in only]
    return pool


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="anthropic",
                        choices=["anthropic", "ollama"])
    parser.add_argument("--model", default=None)
    parser.add_argument("--case", action="append",
                        help="repeatable; run only these")
    parser.add_argument("--reserved", action="store_true",
                        help="the held-out set — do not look at these while tuning")
    parser.add_argument("--keep-world", action="store_true",
                        help="skip the reset; fast, and the results are only "
                             "indicative")
    parser.add_argument("--out", default=str(REPO / "evals" / "results"))
    args = parser.parse_args()

    selected = load_cases(args.reserved, args.case)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stores = out / "stores"
    stores.mkdir(exist_ok=True)

    results = []
    started = time.time()
    for index, case in enumerate(selected, 1):
        print(f"[{index}/{len(selected)}] {case['name']} …", file=sys.stderr,
              flush=True)
        try:
            result = run_case(case, args.provider, args.model, stores,
                              keep_world=args.keep_world)
        except Exception as error:
            result = {"case": case["name"], "family": case["family"],
                      "provider": f"{args.provider}:{args.model}",
                      "verdict": FAIL, "stop_reason": "harness_error",
                      "trace": "", "checks": [{"name": "ran", "ok": False,
                                               "detail": repr(error)}]}
        results.append(result)
        mark = {PASS: "PASS", DECLINED: "DECLINED", FAIL: "FAIL"}[result["verdict"]]
        print(f"    {mark}  ({result['stop_reason']})  "
              f"{result.get('duration_s', 0):.0f}s  "
              f"${result.get('cost_usd', 0):.3f}", file=sys.stderr)
        for check in result["checks"]:
            if not check["ok"]:
                print(f"      x {check['name']}: {check['detail']}",
                      file=sys.stderr)

    label = f"{args.provider}-{args.model or 'default'}"
    if args.reserved:
        label += "-reserved"
    report = out / f"{label}.json"
    report.write_text(json.dumps(results, indent=2))

    print(f"\n{summarise(results)}")
    print(f"\nelapsed {time.time() - started:.0f}s   report {report}")
    return 0 if all(r["verdict"] in (PASS, DECLINED) for r in results) else 1


def summarise(results):
    counts = {PASS: 0, DECLINED: 0, FAIL: 0}
    for result in results:
        counts[result["verdict"]] += 1
    total = len(results)
    good = counts[PASS] + counts[DECLINED]
    lines = [f"{good}/{total} acceptable "
             f"({counts[PASS]} pass, {counts[DECLINED]} correctly declined, "
             f"{counts[FAIL]} fail)"]
    for result in results:
        lines.append(f"  {result['verdict']:9s} {result['case']}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
