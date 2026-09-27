"""Replanning: a vehicle's "no" gets two more plans; nobody else's does.

A scripted model stands in for the planner and the fake gateway for the
vehicle, so each test fails for one reason. What is asserted is how many plans
were made, which one finished, what the replan was told, and that every command
in every plan went through its own approval.
"""
import json

import pytest

from harness.approval import AutoApprovalGate
from harness.executor import Executor
from harness.providers import ModelReply, Provider, ToolCall
from harness.replan import MAX_PLAN_ATTEMPTS, plan_and_run
from harness.trace import Trace
from harness.tests.conftest import RUN, ready_client
from mcp_server.tests.fake_gateway import FakeGateway, free_udp_port


def step(tool, **args):
    return {"tool": tool, "args": {"vehicle_id": "copter_1", **args},
            "rationale": "test", "risk": "high"}


TAKEOFF_ONLY = {"summary": "climb", "steps": [
    step("takeoff", target_altitude_m=15)]}
FULL = {"summary": "climb properly", "steps": [
    step("set_mode", mode_name="GUIDED"), step("arm"),
    step("takeoff", target_altitude_m=15)]}
EMPTY = {"summary": "nothing more I can do", "steps": []}
NO_TOOL = {"summary": "climb", "steps": [
    {"args": {"vehicle_id": "copter_1"}, "rationale": "r", "risk": "low"}]}


class PlanningModel(Provider):
    """Returns the next scripted plan, and keeps what it was asked."""

    name = "scripted"
    model = "planner"

    def __init__(self, plans, before_call=None):
        self.plans = list(plans)
        self.asked = []              # the user message of each call
        self.before_call = before_call

    def complete(self, system, messages, tools):
        self.asked.append(messages[0]["content"])
        number = len(self.asked)
        if self.before_call is not None:
            self.before_call(number)
        plan = self.plans[min(number, len(self.plans)) - 1]
        return ModelReply(tool_calls=[ToolCall(id=f"call-{number}",
                                               name="submit_plan",
                                               arguments=plan)])


class PlanToolBox:
    """The three write tools these plans use, with their real argument shapes."""

    SCHEMAS = {
        "set_mode": {"vehicle_id": "string", "mode_name": "string"},
        "arm": {"vehicle_id": "string"},
        "takeoff": {"vehicle_id": "string", "target_altitude_m": "number"},
    }

    def __contains__(self, name):
        return name in self.SCHEMAS

    def names(self):
        return sorted(self.SCHEMAS)

    def schema(self, name):
        fields = self.SCHEMAS[name]
        return {"type": "object",
                "properties": {k: {"type": v} for k, v in fields.items()},
                "required": list(fields)}

    def definitions(self):
        return [{"name": name, "description": name,
                 "input_schema": self.schema(name)} for name in self.SCHEMAS]


@pytest.fixture
def rejecting():
    """A gateway that refuses every command until a test says otherwise."""
    import vehicle_pb2
    port = free_udp_port()
    gateway = FakeGateway(telemetry_port=port,
                          reject_with=vehicle_pb2.REJECTED_PERMANENT).start()
    client = ready_client(gateway, port)
    yield gateway, client
    client.close()
    gateway.stop()


def run(model, store, client, gate=None, trace=None):
    executor = Executor(store, gate or AutoApprovalGate(grant=True),
                        client=client, trace=trace)
    return plan_and_run("take off to 15 m", model, PlanToolBox(), executor,
                        RUN, trace=trace)


def test_a_plan_that_works_finishes_on_plan_one(store, client):
    mission = run(PlanningModel([FULL]), store, client)

    assert mission.completed
    assert mission.succeeded_on == 1
    assert mission.plan_attempts == 1


def test_a_vehicle_rejection_earns_a_replan_that_can_finish(
        store, rejecting, tmp_path):
    gateway, client = rejecting

    def vehicle_relents(call_number):
        if call_number == 2:           # the vehicle said no to plan 1 only
            gateway.reject_with = None

    model = PlanningModel([TAKEOFF_ONLY, FULL], before_call=vehicle_relents)
    gate = AutoApprovalGate(grant=True)
    trace = Trace(directory=tmp_path, task="take off", provider="scripted")
    mission = run(model, store, client, gate=gate, trace=trace)
    trace.close()

    assert mission.completed
    assert mission.succeeded_on == 2
    assert [a.result.stop_reason for a in mission.attempts] == \
        ["step_rejected", "completed"]

    # The replan was told what ran and why it stopped, in the gateway's words.
    replan_prompt = model.asked[1]
    assert "plan 2 of 3" in replan_prompt
    assert "step_rejected" in replan_prompt
    assert "takeoff" in replan_prompt and "fake gateway" in replan_prompt

    # Nothing rode on plan 1's approval: every write asked again.
    assert [a.tool for a in gate.seen] == ["takeoff", "arm", "takeoff"]

    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    finished = [(e["plan_attempt"], e["stop_reason"])
                for e in events if e["event"] == "plan_finished"]
    assert finished == [(1, "step_rejected"), (2, "completed")]
    assert [e["plan_attempt"] for e in events
            if e["event"] == "plan_proposed"] == [1, 2]


def test_the_budget_is_the_original_plan_and_two_more(store, rejecting):
    _, client = rejecting
    model = PlanningModel([TAKEOFF_ONLY])

    mission = run(model, store, client)

    assert mission.plan_attempts == MAX_PLAN_ATTEMPTS == 3
    assert mission.stop_reason == "step_rejected"
    assert mission.succeeded_on is None
    assert len(model.asked) == 3, "no fourth plan"


def test_an_operator_refusal_is_final(store, client):
    model = PlanningModel([FULL])

    mission = run(model, store, client, gate=AutoApprovalGate(grant=False))

    assert mission.stop_reason == "refused"
    assert mission.plan_attempts == 1, "asking again would negotiate with 'no'"


def test_a_constraint_block_is_final(store, client):
    too_high = {"summary": "climb", "steps": [
        step("set_mode", mode_name="GUIDED"), step("arm"),
        step("takeoff", target_altitude_m=400)]}
    model = PlanningModel([too_high])

    mission = run(model, store, client)

    assert mission.stop_reason == "blocked"
    assert mission.plan_attempts == 1


def test_an_unusable_first_plan_is_no_plan(store, client):
    mission = run(PlanningModel([NO_TOOL]), store, client)

    assert mission.stop_reason == "no_plan"
    assert mission.first_plan.malformed


def test_an_empty_replan_is_a_decline_not_a_malformed_plan(store, rejecting):
    _, client = rejecting
    mission = run(PlanningModel([TAKEOFF_ONLY, EMPTY]), store, client)

    assert mission.stop_reason == "replan_declined"
    assert mission.plan_attempts == 2
