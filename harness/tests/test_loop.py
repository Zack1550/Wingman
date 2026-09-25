"""The guards, proved against a model that does exactly what we tell it to.

Nothing here talks to a model, a gateway or an aircraft. A scripted provider
stands in for the model and an in-memory toolbox for MCP, so every test fails
for one reason only: the loop is wrong.
"""
import json
import time

import pytest

from harness.loop import Budget, Result, run
from harness.providers import ScriptedProvider, ToolCall
from harness.trace import Trace

TAKEOFF_SCHEMA = {
    "type": "object",
    "properties": {"vehicle_id": {"type": "string"},
                   "target_altitude_m": {"type": "number"}},
    "required": ["vehicle_id", "target_altitude_m"],
}
TELEMETRY_SCHEMA = {
    "type": "object",
    "properties": {"vehicle_id": {"type": "string"}},
    "required": ["vehicle_id"],
}


class FakeToolBox:
    """A registry and a dispatcher, with none of the MCP machinery."""

    def __init__(self, results=None, raises=None):
        self._schemas = {"takeoff": TAKEOFF_SCHEMA,
                         "get_telemetry": TELEMETRY_SCHEMA}
        self._results = results or {}
        self._raises = raises or {}
        self.dispatched = []          # (name, arguments) actually executed

    def names(self):
        return sorted(self._schemas)

    def __contains__(self, name):
        return name in self._schemas

    def schema(self, name):
        return self._schemas[name]

    def definitions(self):
        return [{"name": name, "description": f"does {name}",
                 "input_schema": schema}
                for name, schema in self._schemas.items()]

    def call(self, name, arguments):
        self.dispatched.append((name, arguments))
        if name in self._raises:
            raise self._raises[name]
        return self._results.get(name, {"status": "accepted"})


def tool_results(result):
    return [json.loads(m["content"]) for m in result.messages
            if m["role"] == "tool"]


# --- the budgets -----------------------------------------------------------

def test_a_runaway_model_is_stopped_by_the_step_budget():
    """The test the whole guard exists for: a model that never stops."""
    forever = ScriptedProvider(
        [[("takeoff", {"vehicle_id": "copter_1", "target_altitude_m": 10.0})]],
        repeat_last=True)
    toolbox = FakeToolBox()

    result = run("fly", forever, toolbox, Budget(max_model_steps=5))

    assert result.stop_reason == "step_budget"
    assert result.steps == 5
    assert not result.completed


def test_tool_call_budget_stops_dispatch_but_not_the_conversation():
    many = ScriptedProvider(
        [[("takeoff", {"vehicle_id": "copter_1", "target_altitude_m": 10.0})] * 4],
        repeat_last=True)
    toolbox = FakeToolBox()

    result = run("fly", many, toolbox, Budget(max_model_steps=3,
                                              max_tool_calls=2))

    assert len(toolbox.dispatched) == 2, "no dispatch past the budget"
    refusals = [r for r in tool_results(result)
                if r.get("error") == "tool_call_budget_exhausted"]
    assert refusals, "the model must be told, not silently ignored"
    assert "advice" in refusals[0]


def test_deadline_stops_a_slow_run():
    class Slow(ScriptedProvider):
        def complete(self, system, messages, tools):
            time.sleep(0.15)
            return super().complete(system, messages, tools)

    slow = Slow([[("get_telemetry", {"vehicle_id": "copter_1"})]],
                repeat_last=True)
    result = run("watch", slow, FakeToolBox(),
                 Budget(max_model_steps=100, deadline_s=0.3))

    assert result.stop_reason == "deadline"
    assert result.steps < 100


def test_budget_is_checked_before_spending_not_after():
    counter = ScriptedProvider([[("get_telemetry", {"vehicle_id": "copter_1"})]],
                               repeat_last=True)
    run("watch", counter, FakeToolBox(), Budget(max_model_steps=3))
    assert len(counter.calls_seen) == 3, "must not overshoot the step budget"


# --- rejecting what the model made up --------------------------------------

def test_unknown_tool_is_refused_and_explained():
    provider = ScriptedProvider([
        [("self_destruct", {"vehicle_id": "copter_1"})],
        "I could not do that.",
    ])
    toolbox = FakeToolBox()

    result = run("break things", provider, toolbox)

    assert toolbox.dispatched == [], "an invented tool must never dispatch"
    refusal = tool_results(result)[0]
    assert refusal["error"] == "unknown_tool"
    assert "takeoff" in refusal["available_tools"], "say what IS available"
    assert result.stop_reason == "model_finished", "the loop survives it"


def test_missing_required_argument_is_refused():
    provider = ScriptedProvider([
        [("takeoff", {"vehicle_id": "copter_1"})],       # no altitude
        "done",
    ])
    toolbox = FakeToolBox()

    result = run("fly", provider, toolbox)

    assert toolbox.dispatched == []
    refusal = tool_results(result)[0]
    assert refusal["error"] == "invalid_arguments"
    assert "target_altitude_m" in refusal["reason"]


def test_wrong_argument_type_is_refused():
    provider = ScriptedProvider([
        [("takeoff", {"vehicle_id": 7, "target_altitude_m": 10.0})],
        "done",
    ])
    toolbox = FakeToolBox()
    result = run("fly", provider, toolbox)

    assert toolbox.dispatched == []
    assert "vehicle_id" in tool_results(result)[0]["reason"]


def test_a_model_invented_op_id_is_refused():
    """Host-owned fields stay host-owned, even if the model tries to set one."""
    provider = ScriptedProvider([
        [("takeoff", {"vehicle_id": "copter_1", "target_altitude_m": 10.0,
                      "op_id": "op-i-made-this-up"})],
        "done",
    ])
    toolbox = FakeToolBox()

    result = run("fly", provider, toolbox)

    assert toolbox.dispatched == [], "an extra field must not reach the tool"
    refusal = tool_results(result)[0]
    assert refusal["error"] == "invalid_arguments"
    assert "op_id" in refusal["reason"]


def test_a_number_sent_as_a_string_is_repaired():
    """Small models do this constantly; a round trip to scold them is waste."""
    provider = ScriptedProvider([
        [("takeoff", {"vehicle_id": "copter_1", "target_altitude_m": "15"})],
        "done",
    ])
    toolbox = FakeToolBox()

    run("fly", provider, toolbox)

    assert toolbox.dispatched == [("takeoff", {"vehicle_id": "copter_1",
                                               "target_altitude_m": 15.0})]


def test_unrepairable_string_is_still_refused():
    provider = ScriptedProvider([
        [("takeoff", {"vehicle_id": "copter_1", "target_altitude_m": "high"})],
        "done",
    ])
    toolbox = FakeToolBox()
    result = run("fly", provider, toolbox)

    assert toolbox.dispatched == []
    assert tool_results(result)[0]["error"] == "invalid_arguments"


# --- tools that misbehave --------------------------------------------------

def test_a_tool_that_raises_does_not_kill_the_run():
    provider = ScriptedProvider([
        [("takeoff", {"vehicle_id": "copter_1", "target_altitude_m": 10.0})],
        "I reported the failure.",
    ])
    toolbox = FakeToolBox(raises={"takeoff": RuntimeError("socket exploded")})

    result = run("fly", provider, toolbox)

    failure = tool_results(result)[0]
    assert failure["error"] == "tool_failed"
    assert "socket exploded" in failure["reason"]
    assert result.stop_reason == "model_finished"


def test_structured_tool_results_reach_the_model_intact():
    payload = {"status": "unknown", "op_id": "op-abc", "retryable": True,
               "reason": "no ack"}
    provider = ScriptedProvider([
        [("takeoff", {"vehicle_id": "copter_1", "target_altitude_m": 10.0})],
        "done",
    ])
    result = run("fly", provider, FakeToolBox(results={"takeoff": payload}))

    assert tool_results(result)[0] == payload


# --- the ordinary case -----------------------------------------------------

def test_a_model_that_finishes_reports_its_answer():
    provider = ScriptedProvider([
        [("get_telemetry", {"vehicle_id": "copter_1"})],
        "copter_1 is holding at 15 m.",
    ])
    result = run("what is it doing?", provider, FakeToolBox())

    assert result.completed
    assert result.final_text == "copter_1 is holding at 15 m."
    assert result.steps == 2
    assert result.tool_calls == 1


def test_the_model_is_offered_every_tool():
    provider = ScriptedProvider(["nothing to do"])
    run("idle", provider, FakeToolBox())
    assert set(provider.calls_seen[0]["tools"]) == {"takeoff", "get_telemetry"}


def test_conversation_grows_by_request_then_result():
    provider = ScriptedProvider([
        [("get_telemetry", {"vehicle_id": "copter_1"})],
        "done",
    ])
    result = run("check", provider, FakeToolBox())
    assert [m["role"] for m in result.messages] == ["user", "assistant", "tool"]


# --- evidence --------------------------------------------------------------

def test_every_step_is_traced(tmp_path):
    provider = ScriptedProvider([
        [("takeoff", {"vehicle_id": "copter_1", "target_altitude_m": 10.0})],
        [("nope", {})],
        "done",
    ])
    trace = Trace(directory=tmp_path, task="fly", provider="scripted")
    result = run("fly", provider, FakeToolBox(), trace=trace)
    trace.close(stop_reason=result.stop_reason)

    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    kinds = [event["event"] for event in events]

    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_finished"
    assert "tool_result" in kinds
    assert "tool_refused" in kinds, "a refusal is evidence too"
    assert all(event["run_id"] == trace.run_id for event in events)

    dispatched = next(e for e in events if e["event"] == "tool_result")
    assert dispatched["tool"] == "takeoff"
    assert dispatched["arguments"] == {"vehicle_id": "copter_1",
                                       "target_altitude_m": 10.0}
    assert "latency_ms" in dispatched
