"""Runs an approved plan, and refuses to run anything else.

The executor is host code. It does not consult a model, and no model output
reaches it except as the arguments that were already approved. Between the
operator saying yes and the datagram leaving, it checks one more time that the
command has not changed and the world has not moved — because an approval given
ten seconds ago was given about a situation, not about a string.

Writes go through GatewayClient rather than the MCP tool layer, because the
executor must choose the op_id: an approval names the exact command it covers,
and a retry of that command must reuse its id. MCP has no host-only channel to
pass one through, and putting it in the tool schema would hand the model
control of the very thing that makes approval binding meaningful. The cost of
that choice is that tool-body constraints (geofence, altitude cap) do not sit
on this path — they belong in a module both paths call, which is the next
block's job.
"""
import time
from dataclasses import dataclass, field

from harness.approval import needs_approval
from harness.constraints import DEFAULT_CONSTRAINTS
from harness.store import (APPLIED, AWAITING_APPROVAL, EXPIRED, FAILED,
                           REJECTED, SUBMITTED, UNKNOWN, canonical_args)
from vehicle_gateway.client import GatewayClient, GatewayUnavailable

# MCP tool name -> the action name GatewayClient.build understands.
WRITE_TOOLS = {
    "arm": "arm",
    "disarm": "disarm",
    "set_mode": "set_mode",
    "takeoff": "takeoff",
    "goto": "goto_position",
    "return_to_launch": "return_to_launch",
    "land": "land",
}

# GatewayClient.build takes different keyword names than the model-facing tools.
ARGUMENT_NAMES = {
    "goto": {"latitude_deg": "latitude_deg", "longitude_deg": "longitude_deg",
             "altitude_m": "altitude_m"},
}

DEFAULT_APPROVAL_TTL_S = 120
# A retryable rejection means "not yet", and the commonest one — pre-arm checks
# while the position estimate settles — clears in 30-90 s. Treating it as
# terminal turns a normal wait into a failed mission.
DEFAULT_RETRY_ATTEMPTS = 5
DEFAULT_RETRY_WAIT_S = 8.0


@dataclass
class StepOutcome:
    index: int
    tool: str
    args: dict
    status: str              # applied | failed | unknown | rejected | expired
                             # | blocked | read
    reason: str = ""
    op_id: str = ""
    approval_id: str = ""
    result: dict = field(default_factory=dict)


@dataclass
class ExecutionResult:
    run_id: str
    stop_reason: str          # completed | refused | blocked | binding_broken
                              # | error
    outcomes: list = field(default_factory=list)

    @property
    def completed(self):
        return self.stop_reason == "completed"

    def describe(self):
        lines = [f"execution: {self.stop_reason}"]
        for outcome in self.outcomes:
            lines.append(f"  {outcome.index}. {outcome.tool:18s} "
                         f"{outcome.status:9s} {outcome.reason[:70]}")
        return "\n".join(lines)


class Executor:

    def __init__(self, store, gate, client=None, toolbox=None, trace=None,
                 approval_ttl_s=DEFAULT_APPROVAL_TTL_S,
                 retry_attempts=DEFAULT_RETRY_ATTEMPTS,
                 retry_wait_s=DEFAULT_RETRY_WAIT_S,
                 constraints=DEFAULT_CONSTRAINTS):
        self.store = store
        self.gate = gate
        self.client = client or GatewayClient().start()
        self.toolbox = toolbox
        self.trace = trace
        self.approval_ttl_s = approval_ttl_s
        self.retry_attempts = retry_attempts
        self.retry_wait_s = retry_wait_s
        self.constraints = constraints
        # The authoritative state_version per vehicle. Telemetry lags the
        # command path by up to a broadcast interval, so an ack's version is
        # fresher than anything a read can tell us and is preferred when we
        # have one.
        self._version_from_ack = {}

    # -- state ---------------------------------------------------------------

    def current_version(self, vehicle_id):
        from_ack = self._version_from_ack.get(vehicle_id)
        try:
            from_telemetry = self.client.telemetry(vehicle_id)["state_version"]
        except GatewayUnavailable:
            from_telemetry = None
        if from_ack is None:
            if from_telemetry is None:
                raise GatewayUnavailable(
                    f"cannot establish state_version for {vehicle_id}")
            return from_telemetry
        if from_telemetry is None:
            return from_ack
        return max(from_ack, from_telemetry)

    def fleet_or_empty(self):
        try:
            return self.client.fleet_snapshot()
        except GatewayUnavailable:
            return {}

    def telemetry_or_none(self, vehicle_id):
        try:
            return self.client.telemetry(vehicle_id)
        except GatewayUnavailable:
            return None

    # -- the run -------------------------------------------------------------

    def run(self, plan, run_id):
        outcomes = []
        for index, step in enumerate(plan.steps, 1):
            try:
                outcome = self._run_step(index, step, run_id)
            except GatewayUnavailable as error:
                outcomes.append(StepOutcome(index, step.tool, step.args,
                                            "failed", str(error)))
                return ExecutionResult(run_id, "error", outcomes)
            outcomes.append(outcome)

            if outcome.status == "blocked":
                # Not a refusal by a person and not a broken binding: a limit
                # the mission was never allowed to cross.
                return ExecutionResult(run_id, "blocked", outcomes)
            if outcome.status == "rejected":
                # A refusal ends the run. Asking again for the same action,
                # or quietly moving to the next step, would both treat "no"
                # as a negotiating position.
                return ExecutionResult(run_id, "refused", outcomes)
            if outcome.status in ("expired", "failed"):
                return ExecutionResult(run_id, "binding_broken", outcomes)
        return ExecutionResult(run_id, "completed", outcomes)

    def _run_step(self, index, step, run_id):
        if step.tool not in WRITE_TOOLS:
            return self._run_read(index, step)

        vehicle_id = step.args.get("vehicle_id")
        version = self.current_version(vehicle_id)
        command = self.store.propose(run_id, vehicle_id, step.tool, step.args,
                                     state_version=version)
        self._trace("command_proposed", index=index, op_id=command.op_id,
                    tool=step.tool, args=step.args, state_version=version)

        # Constraints before approval, deliberately. Asking a human to approve
        # something the system will refuse anyway teaches them the prompt is
        # noise, and a fence is not a question.
        fleet = self.fleet_or_empty()
        refusal = self.constraints.check(step.tool, step.args,
                                         fleet.get(vehicle_id), fleet)
        if refusal is not None:
            self.store.set_state(command.op_id, REJECTED,
                                 reason=f"{refusal.rule}: {refusal.reason}")
            self._trace("command_blocked", index=index, op_id=command.op_id,
                        tool=step.tool, rule=refusal.rule,
                        reason=refusal.reason)
            return StepOutcome(index, step.tool, step.args, "blocked",
                               f"{refusal.rule}: {refusal.reason}",
                               command.op_id, "", refusal.as_dict())

        approval = None
        if needs_approval(step.tool, step.args):
            approval = self.store.request_approval(command,
                                                   ttl_s=self.approval_ttl_s)
            self._trace("approval_requested", index=index,
                        approval_id=approval.approval_id, op_id=command.op_id,
                        tool=step.tool, args=canonical_args(step.args),
                        state_version=version)

            granted, note = self.gate.decide(
                approval, telemetry=self.telemetry_or_none(vehicle_id),
                rationale=step.rationale)
            approval = self.store.decide(approval.approval_id, granted, note)
            self._trace("approval_decided", approval_id=approval.approval_id,
                        granted=granted, op_id=command.op_id)

            if not granted:
                self.store.set_state(command.op_id, REJECTED,
                                     reason="operator refused")
                return StepOutcome(index, step.tool, step.args, "rejected",
                                   "operator refused", command.op_id,
                                   approval.approval_id)

            # Re-check at the last possible moment. Time passed while a human
            # read the prompt, and the aircraft did not stop for them.
            fresh_version = self.current_version(vehicle_id)
            covered, why = approval.covers(step.tool, step.args, fresh_version)
            if not covered:
                self.store.set_state(command.op_id, EXPIRED, reason=why)
                self._trace("approval_invalidated", op_id=command.op_id,
                            approval_id=approval.approval_id, reason=why)
                return StepOutcome(index, step.tool, step.args, "expired", why,
                                   command.op_id, approval.approval_id)

        return self._dispatch(index, step, command, approval)

    def _dispatch(self, index, step, command, approval):
        vehicle_id = command.vehicle_id
        built = self._build(step.tool, step.args,
                            expected_state_version=command.proposed_state_version)

        # Written before the datagram leaves. If the process dies between here
        # and the ack, the record is what lets a restart find out what happened.
        self.store.set_state(command.op_id, SUBMITTED)
        self._trace("command_submitted", index=index, op_id=command.op_id,
                    tool=step.tool, args=step.args)

        # Retries reuse the op_id, which is what makes them free: the gateway
        # deduplicates, so a command that did land is replayed rather than
        # reapplied. Without that, waiting out a pre-arm check would risk
        # flying the manoeuvre twice.
        for attempt in range(1, self.retry_attempts + 1):
            outcome = self.client.send_command(built, operation_id=command.op_id)
            if not (outcome.status == "rejected" and outcome.retryable):
                break
            if attempt == self.retry_attempts:
                break
            self._trace("command_retrying", index=index, op_id=command.op_id,
                        attempt=attempt, reason=outcome.reason,
                        waiting_s=self.retry_wait_s)
            time.sleep(self.retry_wait_s)

        status = {"accepted": APPLIED, "rejected": FAILED,
                  "unknown": UNKNOWN}.get(outcome.status, UNKNOWN)
        self.store.set_state(command.op_id, status, reason=outcome.reason,
                             result_state_version=outcome.state_version)
        if outcome.status == "accepted" and outcome.state_version:
            self._version_from_ack[vehicle_id] = outcome.state_version

        self._trace("command_outcome", index=index, op_id=command.op_id,
                    status=status, reason=outcome.reason,
                    state_version=outcome.state_version,
                    reconciled=outcome.reconciled)

        return StepOutcome(index, step.tool, step.args,
                           {"applied": "applied", "failed": "failed",
                            "unknown": "unknown"}[status],
                           outcome.reason, command.op_id,
                           approval.approval_id if approval else "",
                           outcome.as_dict())

    # Waiting tools report "not yet" rather than failing, and say so in a
    # field of their own. A step that asked to wait has not succeeded until
    # that field is true.
    WAIT_FIELDS = ("reached", "disarmed")

    def _run_read(self, index, step):
        """Reads and waits go through the MCP tools, unapproved and unledgered.

        A read is still a step. Taking its result on trust — as this did until
        a wait_for_altitude returned gateway_unavailable and the mission
        reported success anyway — makes the confirming step decorative.
        """
        if self.toolbox is None or step.tool not in self.toolbox:
            return StepOutcome(index, step.tool, step.args, "failed",
                               f"no tool '{step.tool}' available to the executor")

        for attempt in range(1, self.retry_attempts + 1):
            result = self.toolbox.call(step.tool, step.args)
            if not isinstance(result, dict):
                result = {"result": result}
            self._trace("read_result", index=index, tool=step.tool,
                        args=step.args, result=result, attempt=attempt)

            if "error" in result:
                return StepOutcome(index, step.tool, step.args, "failed",
                                   str(result.get("reason") or result["error"]),
                                   "", "", result)

            waiting = [f for f in self.WAIT_FIELDS if f in result]
            if not waiting:
                return StepOutcome(index, step.tool, step.args, "read", "",
                                   "", "", result)
            if all(result[f] for f in waiting):
                return StepOutcome(index, step.tool, step.args, "read", "",
                                   "", "", result)
            if attempt == self.retry_attempts:
                return StepOutcome(
                    index, step.tool, step.args, "failed",
                    str(result.get("advice")
                        or f"{waiting[0]} was still false after "
                           f"{self.retry_attempts} attempts"),
                    "", "", result)
            self._trace("read_retrying", index=index, tool=step.tool,
                        attempt=attempt, result=result)

    @staticmethod
    def _build(tool, args, expected_state_version=0):
        action = WRITE_TOOLS[tool]
        fields = {k: v for k, v in args.items() if k != "vehicle_id"}
        if expected_state_version:
            fields["expected_state_version"] = expected_state_version
        return GatewayClient.build(args["vehicle_id"], action, **fields)

    def _trace(self, event, **fields):
        if self.trace is not None:
            self.trace.write(event, **fields)


# --- recovery ---------------------------------------------------------------

def reconcile_outstanding(store, client, trace=None):
    """What a restarting harness must do before it does anything else.

    Every command left in submitted or unknown may or may not have reached the
    vehicle. The gateway remembers; ask it, rather than re-sending and flying
    the manoeuvre twice.
    """
    resolved = []
    for command in store.outstanding():
        try:
            status = client.command_status(command.op_id, raise_if_silent=False)
        except GatewayUnavailable:
            status = None

        if status is None:
            store.set_state(command.op_id, UNKNOWN,
                            reason="gateway did not answer a status query")
            outcome = "still_unknown"
        elif not status["is_known"]:
            store.set_state(command.op_id, FAILED,
                            reason="gateway never saw this operation; it did "
                                   "not arrive and was never applied")
            outcome = "never_arrived"
        else:
            mapped = {"accepted": APPLIED, "rejected": FAILED,
                      "unknown": UNKNOWN}.get(status["status"], UNKNOWN)
            store.set_state(command.op_id, mapped, reason=status["reason"],
                            result_state_version=status.get("state_version", 0))
            outcome = mapped

        resolved.append((command.op_id, outcome))
        if trace is not None:
            trace.write("reconciled", op_id=command.op_id, outcome=outcome,
                        tool=command.tool)
    return resolved
