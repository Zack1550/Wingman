"""The agent loop: ask, check, execute, append, repeat.

Everything the model produces is untrusted input that happens to be well
formatted. So every path through this file that could reject a tool call returns
a dict to the model instead of raising: an unknown tool, a bad argument, an
exhausted budget and a tool that threw all become the next message, which is the
only form in which a model can recover from them.

Two rules the shape of this file enforces:

  The budget is checked before we spend, not after. A loop that notices it has
  overrun has already overrun.

  Nothing is ever dispatched by name off model output. No eval, no getattr: a
  tool call is looked up in a registry the host built, or it is refused.
"""
import json
import time
from dataclasses import dataclass, field

from harness.trace import NullTrace
from harness.validate import coerce_arguments, validate_arguments

SYSTEM_PROMPT = """You command simulated multirotor aircraft through tools.

How this vehicle works:
- A vehicle must be in GUIDED mode and armed before it can take off.
- Arming is often refused for the first 30-90 seconds after startup while the
  position estimate settles. That refusal is normal: wait, then try again.
- An armed aircraft left on the ground disarms itself after about 10 seconds.

How to decide what happened:
- A tool result of "accepted" means the aircraft took the command, NOT that the
  manoeuvre finished. Confirm every effect with get_telemetry before you report
  it or move on. Telemetry is the only ground truth.
- "rejected" with retryable true means try again shortly. With retryable false,
  the plan is wrong: change it rather than repeating it.
- "unknown" means the command may or may not have applied. Never guess. Call
  get_command_status with the op_id you were given. If that also fails, stop and
  report the uncertainty rather than re-issuing the command.
- An "error" of gateway_unavailable means you cannot see the vehicle. Stop and
  say so. Do not assume anything about its state.

Work in small steps: one or two tool calls, look at what came back, then decide.
When the task is done, reply in plain text with what you did and what telemetry
confirmed. Do not claim anything telemetry did not show you."""


@dataclass
class Budget:
    """What the run is allowed to spend before it is stopped."""
    max_model_steps: int = 12
    max_tool_calls: int = 30
    deadline_s: float = 300.0


@dataclass
class Result:
    stop_reason: str          # model_finished | step_budget | tool_budget | deadline
    final_text: str = ""
    steps: int = 0
    tool_calls: int = 0
    messages: list = field(default_factory=list)
    run_id: str = ""
    trace_path: str = ""

    @property
    def completed(self):
        return self.stop_reason == "model_finished"


def run(task, provider, toolbox, budget=None, trace=None, system=SYSTEM_PROMPT):
    budget = budget or Budget()
    trace = trace or NullTrace()
    definitions = toolbox.definitions()

    messages = [{"role": "user", "content": task}]
    started = time.time()
    steps = 0
    tool_calls = 0

    def finish(stop_reason, final_text=""):
        trace.write("stopped", stop_reason=stop_reason, steps=steps,
                    tool_calls=tool_calls)
        return Result(stop_reason=stop_reason, final_text=final_text,
                      steps=steps, tool_calls=tool_calls, messages=messages,
                      run_id=getattr(trace, "run_id", ""),
                      trace_path=str(getattr(trace, "path", "") or ""))

    while True:
        # Guards first. Spending is what we are trying to bound, so the check
        # goes before the spend.
        if steps >= budget.max_model_steps:
            return finish("step_budget")
        elapsed = time.time() - started
        if elapsed > budget.deadline_s:
            return finish("deadline")

        steps += 1
        model_started = time.time()
        reply = provider.complete(system, messages, definitions)
        latency_ms = int((time.time() - model_started) * 1000)

        trace.write("model_reply", step=steps, latency_ms=latency_ms,
                    model_latency_ms=latency_ms,
                    text=reply.text, thinking=reply.thinking,
                    tool_calls=[{"name": call.name, "arguments": call.arguments}
                                for call in reply.tool_calls],
                    usage=reply.usage)

        if not reply.wants_tools:
            return finish("model_finished", reply.text)

        messages.append({"role": "assistant", "content": reply.text,
                         "tool_calls": reply.tool_calls,
                         "provider_blocks": reply.provider_blocks})

        for call in reply.tool_calls:
            if tool_calls >= budget.max_tool_calls:
                # Told, not crashed: the model still gets a turn to wrap up.
                messages.append(_tool_message(call, {
                    "error": "tool_call_budget_exhausted",
                    "reason": f"this run may make at most "
                              f"{budget.max_tool_calls} tool calls",
                    "advice": "Stop and report what you have confirmed so far.",
                }))
                trace.write("tool_refused", step=steps, tool=call.name,
                            reason="tool_call_budget_exhausted")
                continue

            tool_calls += 1
            result = _dispatch(toolbox, call, trace, steps)
            messages.append(_tool_message(call, result))

    # unreachable


def _dispatch(toolbox, call, trace, step):
    """Look it up, check it, run it. Every failure returns, none raises."""
    if call.name not in toolbox:
        trace.write("tool_refused", step=step, tool=call.name,
                    reason="unknown_tool")
        return {"error": "unknown_tool",
                "reason": f"there is no tool called '{call.name}'",
                "available_tools": toolbox.names()}

    schema = toolbox.schema(call.name)
    arguments = coerce_arguments(call.arguments, schema)
    problem = validate_arguments(arguments, schema)
    if problem is not None:
        trace.write("tool_refused", step=step, tool=call.name,
                    arguments=call.arguments, reason=problem)
        return {"error": "invalid_arguments", "reason": problem}

    started = time.time()
    try:
        result = toolbox.call(call.name, arguments)
    except Exception as error:
        # A tool that throws is a harness failure, not a mission failure, but
        # the model is still the one that has to cope with it.
        trace.write("tool_failed", step=step, tool=call.name,
                    arguments=arguments, reason=repr(error))
        return {"error": "tool_failed", "reason": str(error)}

    latency_ms = int((time.time() - started) * 1000)
    trace.write("tool_result", step=step, tool=call.name, arguments=arguments,
                result=result, latency_ms=latency_ms)
    return result


def _tool_message(call, result):
    return {"role": "tool", "tool_call_id": call.id, "name": call.name,
            "content": json.dumps(result, default=str)}
