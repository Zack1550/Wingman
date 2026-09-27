"""Plan, run, and when the vehicle says no, plan again — a bounded number of times.

The executor stops at the first failed step, and it is right to: it runs what was
approved and nothing else. But a stop is not always the end of the mission. When
the vehicle refuses a write it usually says why and what to do instead — "arm
first, then take off", "use a goto at the current position" — and a plan made
without that information can be fixed by one made with it.

What earns a replan, and what does not:

    step_rejected      the vehicle refused a write        replan
    step_unconfirmed   a read or wait did not confirm     replan
    refused            the operator said no               stop — "no" is final
    blocked            a constraint refused it            stop — a fence is not a question
    approval_void      the approval stopped covering it   stop
    error              the gateway could not be reached   stop

A replan does not widen anything. Each command in a new plan goes through the
same constraints and the same per-command approval as the first plan's did, and
the budget is fixed: the original plan and two more.
"""
from dataclasses import dataclass, field

from harness.planner import describe_situation, make_plan

MAX_PLAN_ATTEMPTS = 3            # the original plan, plus two replans
REPLAN_ON = {"step_rejected", "step_unconfirmed"}


@dataclass
class PlanAttempt:
    number: int                  # 1 is the original plan
    plan: object
    result: object = None        # ExecutionResult, or None if it never ran


@dataclass
class MissionResult:
    stop_reason: str
    attempts: list = field(default_factory=list)

    @property
    def completed(self):
        return self.stop_reason == "completed"

    @property
    def plan_attempts(self):
        """How many plans were made, whether or not they ran."""
        return len(self.attempts)

    @property
    def succeeded_on(self):
        """Which plan finished the mission, or None if none did.

        The number that separates a model that plans correctly from one that
        gets there once the vehicle has told it what it missed. Both finish;
        they are not the same capability.
        """
        return self.attempts[-1].number if self.completed else None

    @property
    def first_plan(self):
        return self.attempts[0].plan if self.attempts else None

    @property
    def last_plan(self):
        return self.attempts[-1].plan if self.attempts else None

    @property
    def outcomes(self):
        """Every step outcome across every plan, in the order they ran."""
        return [(attempt.number, outcome) for attempt in self.attempts
                if attempt.result is not None
                for outcome in attempt.result.outcomes]

    def describe(self):
        lines = []
        for attempt in self.attempts:
            if attempt.result is None:
                lines.append(f"plan {attempt.number}: no usable plan "
                             f"({'; '.join(attempt.plan.problems)})")
                continue
            lines.append(f"plan {attempt.number}: {attempt.result.describe()}")
        lines.append(f"stopped: {self.stop_reason} after "
                     f"{self.plan_attempts} plan(s)")
        return "\n".join(lines)


def plan_and_run(task, provider, toolbox, executor, run_id, trace=None,
                 max_plan_attempts=MAX_PLAN_ATTEMPTS, on_plan=None):
    """Plan against the current state, execute, and replan on a vehicle "no".

    `on_plan(plan, number)` is called before each plan runs, so a console can
    show the operator what they are about to be asked to approve.
    """
    attempts = []
    for number in range(1, max_plan_attempts + 1):
        # Fresh every time: after a partial run the aircraft is not where the
        # last plan found it, and a replan made from the old picture would be
        # the same mistake with better intentions.
        situation = describe_situation(executor.client)
        if trace is not None:
            trace.write("situation", source="gateway", plan_attempt=number,
                        situation=situation)
        plan = make_plan(task, provider, toolbox, trace, situation=situation,
                         history=attempts, plan_attempt=number,
                         max_plan_attempts=max_plan_attempts)

        if not plan.is_valid:
            attempts.append(PlanAttempt(number, plan))
            if number == 1:
                stop_reason = "no_plan"
            elif not plan.malformed and plan.problems == ["the plan has no steps"]:
                # An empty replan is the model saying there is nothing left it
                # can do, which is an answer, not a malformed plan.
                stop_reason = "replan_declined"
            else:
                stop_reason = "replan_invalid"
            _finished(trace, number, stop_reason, None)
            return MissionResult(stop_reason, attempts)

        if on_plan is not None:
            on_plan(plan, number)
        result = executor.run(plan, run_id)
        attempts.append(PlanAttempt(number, plan, result))
        _finished(trace, number, result.stop_reason, result)

        if result.stop_reason not in REPLAN_ON:
            return MissionResult(result.stop_reason, attempts)

    # Out of plans. The last plan's reason is the honest one; plan_attempts
    # equal to the budget says the replans were spent.
    return MissionResult(attempts[-1].result.stop_reason, attempts)


def _finished(trace, number, stop_reason, result):
    if trace is None:
        return
    last = result.outcomes[-1] if result is not None and result.outcomes else None
    trace.write("plan_finished", source="harness", plan_attempt=number,
                stop_reason=stop_reason,
                stopped_at=({"index": last.index, "tool": last.tool,
                             "status": last.status, "reason": last.reason}
                            if last is not None and stop_reason != "completed"
                            else None))
