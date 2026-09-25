"""Who says yes, and how.

The single rule this file exists to enforce: approval is an application event,
never a phrase the model interprets. Nothing a model writes — in a plan, in a
tool result, in a retrieved document claiming the operator pre-approved
everything — reaches this code path. A decision arrives from an operator at a
keyboard, or from a policy the host chose, and nowhere else.

The gate answers one question: grant this specific command, or refuse it. The
binding (exact arguments, telemetry version, expiry) is checked separately, by
the store, immediately before dispatch — so a decision made here cannot widen
later.
"""
import sys

from harness.store import canonical_args

# Tools that move an aircraft or make it able to move. Everything here needs a
# human before it runs; everything else is a read or a harmless mode change.
RISKY_TOOLS = {"arm", "takeoff", "goto", "land", "return_to_launch", "disarm"}

# set_mode is only risky for modes that fly the aircraft on their own.
FLYING_MODES = {"RTL", "LAND", "AUTO", "AUTO_RTL", "SMART_RTL", "BRAKE"}


def needs_approval(tool, args):
    if tool in RISKY_TOOLS:
        return True
    if tool == "set_mode":
        return str(args.get("mode_name", "")).upper() in FLYING_MODES
    return False


class ApprovalGate:
    """Base class. Subclasses decide; none of them ask a model."""

    def decide(self, approval, telemetry=None, rationale=""):
        raise NotImplementedError


class ConsoleApprovalGate(ApprovalGate):
    """A human at a keyboard, which is the only kind that counts on Tuesday."""

    def __init__(self, stream=None, prompt_stream=None):
        self._in = stream or sys.stdin
        self._out = prompt_stream or sys.stderr

    def decide(self, approval, telemetry=None, rationale=""):
        write = self._out.write
        write("\n" + "=" * 68 + "\n")
        write(f"  APPROVAL REQUESTED   {approval.approval_id}\n")
        write("=" * 68 + "\n")
        write(f"  vehicle        {approval.vehicle_id}\n")
        write(f"  command        {approval.tool}\n")
        write(f"  arguments      {canonical_args(approval.args)}\n")
        write(f"  op_id          {approval.op_id}\n")
        write(f"  state_version  {approval.proposed_state_version}\n")
        if rationale:
            write(f"  model says     {rationale}\n")
        if telemetry:
            write(f"  telemetry      alt {telemetry.get('altitude_m')} m, "
                  f"{telemetry.get('flight_mode')}, "
                  f"armed={telemetry.get('is_armed')}, "
                  f"battery {telemetry.get('battery_percent')}%\n")
        write("-" * 68 + "\n")
        write("  approve this exact command? [y/N] ")
        self._out.flush()

        try:
            answer = self._in.readline().strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        granted = answer in ("y", "yes")
        write(f"  -> {'GRANTED' if granted else 'REFUSED'}\n\n")
        self._out.flush()
        return granted, answer


class AutoApprovalGate(ApprovalGate):
    """For tests and dry runs. Never wire this to anything that can fly."""

    def __init__(self, grant=True):
        self.grant = grant
        self.seen = []

    def decide(self, approval, telemetry=None, rationale=""):
        self.seen.append(approval)
        return self.grant, "auto"


class ScriptedApprovalGate(ApprovalGate):
    """Decisions in order, so a test can approve one step and refuse the next."""

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.seen = []
        self._index = 0

    def decide(self, approval, telemetry=None, rationale=""):
        self.seen.append(approval)
        if self._index < len(self.decisions):
            granted = self.decisions[self._index]
            self._index += 1
        else:
            granted = False
        return granted, "scripted"
