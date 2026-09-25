#!/usr/bin/env python3
"""Merge a telemetry capture and an agent trace into one scrubbable timeline.

    python -m observability.build_mcap \\
        --telemetry captures/mission.jsonl \\
        --trace traces/run-....jsonl \\
        --out captures/mission.mcap

Then open the .mcap in Foxglove. The point of putting both streams in one file
is causality: the aircraft climbing and the tool call that told it to are on the
same clock, so you can scrub to a manoeuvre and see what caused it — including
the ones nobody intended.

Topics written:

    /copter_N/gps          LocationFix   map and 3D view
    /copter_N/state        JSON          altitude, battery, mode, state_version
    /agent/log             Log           one human-readable line per event
    /agent/tool_calls      JSON          arguments and results, inspectable
    /agent/approvals       JSON          requested, decided, invalidated
    /agent/commands        JSON          submitted, outcome, blocked, reconciled

Both streams are stamped with the same wall clock. The trace records
at_unix_ms per event and the capture records arrival time, so they interleave
correctly without any alignment step.
"""
import argparse
import json
from pathlib import Path

import foxglove
from foxglove.channels import LocationFixChannel, LogChannel
from foxglove.messages import LocationFix, Log, LogLevel

MS_TO_NS = 1_000_000

# Which trace events belong on which agent topic, and how to say them in one
# line. Everything else still reaches /agent/log as raw JSON — a log you have
# to edit before it shows you an unexpected event is not much of a log.
TOOL_EVENTS = {"tool_result", "read_result", "tool_refused", "tool_failed",
               "read_retrying"}
APPROVAL_EVENTS = {"approval_requested", "approval_decided",
                   "approval_invalidated"}
COMMAND_EVENTS = {"command_proposed", "command_submitted", "command_outcome",
                  "command_blocked", "command_retrying", "reconciled"}

STATE_SCHEMA = {
    "type": "object",
    "properties": {
        "altitude_m": {"type": "number"},
        "ground_speed_mps": {"type": "number"},
        "heading_deg": {"type": "number"},
        "battery_percent": {"type": "number"},
        "gps_fix_type": {"type": "number"},
        "state_version": {"type": "number"},
        "is_armed": {"type": "boolean"},
        "flight_mode": {"type": "string"},
    },
}
EVENT_SCHEMA = {"type": "object", "additionalProperties": True}


def one_line(event):
    """A readable summary, because a timeline of JSON blobs is not a timeline."""
    kind = event.get("event", "?")
    if kind == "plan_proposed":
        steps = event.get("steps") or []
        return (f"PLAN ({len(steps)} steps): "
                + " -> ".join(s.get("tool", "?") for s in steps))
    if kind == "approval_requested":
        return f"APPROVAL? {event.get('tool')} {event.get('args', '')}"
    if kind == "approval_decided":
        return (f"APPROVAL {'GRANTED' if event.get('granted') else 'REFUSED'}"
                f" {event.get('op_id', '')}")
    if kind == "approval_invalidated":
        return f"APPROVAL VOID {event.get('reason', '')}"
    if kind == "command_submitted":
        return f"SEND {event.get('tool')} {event.get('args', '')}"
    if kind == "command_outcome":
        return (f"ACK {event.get('tool') or ''} {event.get('status')} "
                f"v{event.get('state_version', '')} {event.get('reason', '')}")
    if kind == "command_blocked":
        return f"BLOCKED {event.get('rule')}: {event.get('reason', '')}"
    if kind == "command_retrying":
        return f"RETRY {event.get('op_id')} attempt {event.get('attempt')}"
    if kind == "reconciled":
        return f"RECONCILED {event.get('op_id')} -> {event.get('outcome')}"
    if kind in ("tool_result", "read_result"):
        return f"TOOL {event.get('tool')} -> {json.dumps(event.get('result'))[:160]}"
    if kind in ("tool_refused", "tool_failed"):
        return f"TOOL REFUSED {event.get('tool')}: {event.get('reason', '')}"
    if kind == "model_reply":
        return f"MODEL step {event.get('step')} ({event.get('latency_ms', 0)} ms)"
    if kind == "mission_report":
        return f"REPORT {(event.get('report') or '')[:200]}"
    if kind == "situation":
        return f"SITUATION {(event.get('situation') or '')[:200]}"
    return f"{kind} {json.dumps({k: v for k, v in event.items() if k not in ('run_id', 'event', 'at_unix_ms', 'elapsed_s')})[:200]}"


def level_for(event):
    kind = event.get("event", "")
    if kind in ("command_blocked", "tool_failed", "approval_invalidated"):
        return LogLevel.Error
    if kind in ("tool_refused", "command_retrying", "read_retrying"):
        return LogLevel.Warn
    return LogLevel.Info


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def build(telemetry_path, trace_paths, out_path):
    samples = load_jsonl(telemetry_path) if telemetry_path else []
    events = []
    for path in trace_paths or []:
        events.extend(load_jsonl(path))

    if not samples and not events:
        raise SystemExit("nothing to write: no telemetry and no trace")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with foxglove.open_mcap(str(out_path), allow_overwrite=True):
        gps = {}
        state = {}
        agent_log = LogChannel("/agent/log")
        topics = {
            "tools": foxglove.Channel("/agent/tool_calls", schema=EVENT_SCHEMA),
            "approvals": foxglove.Channel("/agent/approvals", schema=EVENT_SCHEMA),
            "commands": foxglove.Channel("/agent/commands", schema=EVENT_SCHEMA),
            "other": foxglove.Channel("/agent/events", schema=EVENT_SCHEMA),
        }

        for sample in samples:
            name = sample["vehicle_id"]
            when = int(sample["received_at_unix_ms"]) * MS_TO_NS
            if name not in gps:
                gps[name] = LocationFixChannel(f"/{name}/gps")
                state[name] = foxglove.Channel(f"/{name}/state",
                                               schema=STATE_SCHEMA)
            gps[name].log(
                LocationFix(latitude=sample["latitude_deg"],
                            longitude=sample["longitude_deg"],
                            altitude=sample["altitude_m"]),
                log_time=when)
            state[name].log({
                "altitude_m": sample["altitude_m"],
                "ground_speed_mps": sample["ground_speed_mps"],
                "heading_deg": sample["heading_deg"],
                "battery_percent": sample["battery_percent"],
                "gps_fix_type": sample["gps_fix_type"],
                "state_version": sample["state_version"],
                "is_armed": sample["is_armed"],
                "flight_mode": sample["flight_mode"],
            }, log_time=when)

        for event in events:
            when = int(event.get("at_unix_ms", 0)) * MS_TO_NS
            if not when:
                continue
            agent_log.log(
                Log(level=level_for(event), name=event.get("event", "agent"),
                    message=one_line(event)),
                log_time=when)
            kind = event.get("event")
            if kind in TOOL_EVENTS:
                key = "tools"
            elif kind in APPROVAL_EVENTS:
                key = "approvals"
            elif kind in COMMAND_EVENTS:
                key = "commands"
            else:
                key = "other"
            topics[key].log(event, log_time=when)

    return len(samples), len(events), out_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telemetry", help="capture from record_telemetry")
    parser.add_argument("--trace", action="append",
                        help="repeatable; agent trace JSONL")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    samples, events, path = build(args.telemetry, args.trace, args.out)
    print(f"{samples} telemetry samples + {events} agent events -> {path}")
    print(f"size {path.stat().st_size / 1024:.0f} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
