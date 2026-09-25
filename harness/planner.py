"""The model proposes; it does not act.

A planner run produces one artefact: an ordered list of steps, each naming a
tool and its exact arguments. That artefact is data, checked against the real
tool schemas before anyone sees it, and it is what the operator approves.

The point of the split is not tidiness. A plan in prose can be read two ways; a
plan as data has exactly one reading, and an approval can be bound to it. Every
lesson from tonight's runs — "confirm" interpreted three ways, a tolerance
chosen loosely, an intention announced rather than executed — is a case of a
model's words being treated as a decision. Here they are treated as a proposal.
"""
import json
import time
from dataclasses import dataclass, field

from harness.validate import coerce_arguments, validate_arguments

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One sentence: what this plan achieves overall.",
        },
        "steps": {
            "type": "array",
            "description": "The steps to run, in order.",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string",
                             "description": "Exact name of one available tool."},
                    "args": {"type": "object",
                             "description": "Exact arguments for that tool."},
                    "rationale": {"type": "string",
                                  "description": "Why this step, in one line."},
                    "risk": {"type": "string", "enum": ["low", "high"],
                             "description": "high if it moves the aircraft or "
                                            "makes it able to move."},
                },
                "required": ["tool", "args", "rationale", "risk"],
            },
        },
    },
    "required": ["summary", "steps"],
}

SUBMIT_PLAN_TOOL = {
    "name": "submit_plan",
    "description": ("Submit your plan. This is the only thing you may do: it "
                    "records the plan for an operator to approve. It does not "
                    "run anything and does not touch any aircraft."),
    "input_schema": PLAN_SCHEMA,
}

PLANNER_SYSTEM = """You plan missions for simulated multirotor aircraft. You do
not fly them. You produce a plan, an operator approves it, and a separate
executor runs it.

Call submit_plan exactly once with an ordered list of steps.

What the aircraft requires:
- It must be in GUIDED mode and armed before it can take off.
- Arming is refused while the position estimate settles, for 30-90 s after
  startup. Plan for it; do not plan around it.
- An armed aircraft on the ground disarms itself after about 10 seconds, so
  arm immediately before takeoff, not earlier.
- A takeoff command starts a climb; it does not finish one. If the mission
  needs the aircraft to be at an altitude, include a wait_for_altitude step.
- A goto starts a transit; it does not finish one. If the mission needs the
  aircraft to be somewhere, include a wait_for_position step.
- takeoff only works from the ground. If the aircraft is already flying, a
  request to reach some altitude is a goto at its current latitude and
  longitude with the new altitude, not a takeoff.

Mark a step risk "high" if it moves the aircraft or makes it able to move:
arm, takeoff, goto, land, return_to_launch, disarm, and any set_mode into a
mode that flies itself. Reads are "low".

Use exact tool names and exact argument names. Include every argument the tool
requires. Do not invent arguments that are not in the tool's schema."""


@dataclass
class PlanStep:
    tool: str
    args: dict
    rationale: str = ""
    risk: str = "low"

    @property
    def high_risk(self):
        return self.risk == "high"


@dataclass
class Plan:
    summary: str = ""
    steps: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    problems: list = field(default_factory=list)

    @property
    def is_valid(self):
        return not self.problems and bool(self.steps)

    def describe(self):
        """Mark the steps that will actually be put to the operator.

        The model's own "risk" label is a claim, not a decision — it can call a
        takeoff low-risk or a mode change high-risk and neither changes what
        happens. The marker here comes from the same function the gate uses, so
        what an operator reads matches what they will be asked. A disagreement
        is worth seeing, so it is shown rather than hidden.
        """
        from harness.approval import needs_approval

        lines = [f"PLAN: {self.summary}"]
        for index, step in enumerate(self.steps, 1):
            gated = needs_approval(step.tool, step.args)
            marker = "!" if gated else " "
            note = ""
            if step.high_risk and not gated:
                note = "   [model called this high risk; no approval needed]"
            elif gated and not step.high_risk:
                note = "   [model called this low risk; approval required anyway]"
            lines.append(
                f"  {marker} {index}. {step.tool}({_short_args(step.args)}){note}")
            if step.rationale:
                lines.append(f"        {step.rationale}")
        approvals = sum(1 for s in self.steps
                        if needs_approval(s.tool, s.args))
        lines.append(f"\n  {approvals} of {len(self.steps)} steps need your approval")
        return "\n".join(lines)


def _short_args(args):
    return ", ".join(f"{k}={v!r}" for k, v in sorted((args or {}).items()))


def tool_catalogue(toolbox):
    """What the planner is allowed to plan with, as text rather than as tools.

    The planner must not be able to call these — only to name them — so they go
    in the prompt, and the single callable tool is submit_plan.
    """
    lines = []
    for definition in toolbox.definitions():
        if definition["name"] == "submit_plan":
            continue
        properties = definition["input_schema"].get("properties", {})
        required = set(definition["input_schema"].get("required", []))
        arguments = ", ".join(
            f"{name}: {spec.get('type', 'any')}"
            + ("" if name in required else " (optional)")
            for name, spec in properties.items()) or "no arguments"
        headline = (definition["description"] or "").strip().split("\n")[0]
        lines.append(f"- {definition['name']}({arguments})\n    {headline}")
    return "\n".join(lines)


def validate_plan(raw, toolbox):
    """Schema conformance is cheap; correctness is not. Check what we can.

    Every step must name a real tool and pass that tool's own argument schema,
    so a plan cannot be approved and then fail at dispatch for something that
    was visible at planning time.
    """
    problems = []
    steps = []

    if not isinstance(raw, dict):
        return Plan(problems=[f"plan must be an object, got {type(raw).__name__}"])

    for index, entry in enumerate(raw.get("steps") or [], 1):
        if not isinstance(entry, dict):
            problems.append(f"step {index} is not an object")
            continue
        tool = entry.get("tool")
        args = entry.get("args") or {}
        if not tool:
            problems.append(f"step {index} names no tool")
            continue
        if tool not in toolbox:
            problems.append(
                f"step {index} names unknown tool '{tool}'; "
                f"available: {', '.join(toolbox.names())}")
            continue
        args = coerce_arguments(args, toolbox.schema(tool))
        trouble = validate_arguments(args, toolbox.schema(tool))
        if trouble:
            problems.append(f"step {index} ({tool}): {trouble}")
            continue
        steps.append(PlanStep(tool=tool, args=args,
                              rationale=str(entry.get("rationale", "")),
                              risk=str(entry.get("risk", "low")).lower()))

    if not steps and not problems:
        problems.append("the plan has no steps")

    return Plan(summary=str(raw.get("summary", "")), steps=steps, raw=raw,
                problems=problems)


def describe_situation(client, vehicles=None):
    """What the aircraft are doing right now, as text for the planner.

    Without this the planner works from the task alone and will cheerfully plan
    a takeoff for an aircraft that is already airborne. A plan made against an
    imagined world is stale before the operator ever sees it.
    """
    lines = []
    for entry in client.known_vehicles():
        name = entry["vehicle_id"]
        if vehicles and name not in vehicles:
            continue
        if not entry["reachable"]:
            lines.append(f"- {name}: NOT REACHABLE (last heard "
                         f"{entry['last_seen_s_ago']}s ago)")
            continue
        try:
            sample = client.telemetry(name)
        except Exception as error:
            lines.append(f"- {name}: telemetry unavailable ({error})")
            continue
        # Position is here because some commands can only be planned with it.
        # A goto that holds station at a new altitude needs the aircraft's
        # current latitude and longitude, and those are not knowable at
        # planning time unless we supply them — a plan whose arguments are
        # filled in later could not be bound to an approval.
        lines.append(
            f"- {name}: {sample['flight_mode']}, "
            f"armed={sample['is_armed']}, altitude {sample['altitude_m']} m, "
            f"position {sample['latitude_deg']}, {sample['longitude_deg']}, "
            f"heading {sample['heading_deg']}deg, "
            f"battery {sample['battery_percent']}%, "
            f"gps_fix {sample['gps_fix_type']}, "
            f"state_version {sample['state_version']}")
    return "\n".join(lines) or "- no vehicle is broadcasting telemetry"


def make_plan(task, provider, toolbox, trace=None, attempts=2, situation=None):
    """One model turn, validated. A malformed plan gets one chance to be fixed."""
    system = f"{PLANNER_SYSTEM}\n\nTools you may plan with:\n{tool_catalogue(toolbox)}"
    if situation:
        system += (f"\n\nThe aircraft RIGHT NOW:\n{situation}\n"
                   f"Plan from this state, not from a fresh one. Do not plan a "
                   f"step whose effect has already happened, and say so in the "
                   f"summary if the task is already satisfied.\n"
                   f"Every argument in your plan must be a literal value. Use "
                   f"the positions above when a step needs coordinates — you "
                   f"cannot refer to the output of an earlier step, because "
                   f"the operator approves each command exactly as written.")
    messages = [{"role": "user", "content": task}]
    plan = Plan(problems=["the planner produced nothing"])

    for attempt in range(1, attempts + 1):
        started = time.time()
        reply = provider.complete(system, messages, [SUBMIT_PLAN_TOOL])
        model_latency_ms = int((time.time() - started) * 1000)
        call = next((c for c in reply.tool_calls if c.name == "submit_plan"), None)

        if call is None:
            plan = Plan(problems=["the planner did not call submit_plan"])
        else:
            plan = validate_plan(call.arguments, toolbox)

        if trace is not None:
            trace.write("plan_proposed", attempt=attempt,
                        model_latency_ms=model_latency_ms,
                        summary=plan.summary,
                        steps=[{"tool": s.tool, "args": s.args, "risk": s.risk}
                               for s in plan.steps],
                        problems=plan.problems, thinking=reply.thinking,
                        usage=reply.usage)

        if plan.is_valid or attempt == attempts:
            return plan

        # Hand the failure back the same way a tool failure is handed back.
        messages.append({"role": "assistant", "content": reply.text,
                         "tool_calls": reply.tool_calls,
                         "provider_blocks": reply.provider_blocks})
        if call is not None:
            messages.append({
                "role": "tool", "tool_call_id": call.id, "name": "submit_plan",
                "content": json.dumps({"error": "invalid_plan",
                                       "problems": plan.problems})})
        else:
            messages.append({"role": "user",
                             "content": "You must call submit_plan."})
    return plan
