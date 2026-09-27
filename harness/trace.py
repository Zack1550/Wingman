"""One JSONL line per thing that happened.

The model's closing sentence is not evidence. When a run goes wrong the question
is always "what did it actually call, with what, and what came back" — and the
answer has to survive the process. Each line is self-contained so the file can
be tailed, grepped, replayed, or fed to Sunday's MCAP writer and SFT export
without parsing state across lines.

Every line names its source: who produced the information it records, which is
not always the code that wrote the line. The executor writes command_outcome,
but the status and reason in it are the gateway's; it writes approval_decided,
but the decision was an operator's or a policy's. Without this a plan-path trace
is a dozen event types from five components and no way to tell whose words are
whose.

    harness       the harness's own bookkeeping, and conclusions it drew from
                  silence (no ack, no answer to a status query)
    model         text or a plan the model produced
    operator      a person's approval decision
    auto_approval / scripted_approval
                  a policy deciding in the operator's place (evals, tests)
    approval_gate a gate that did not say which kind it is
    executor      a decision the executor made: propose, submit, retry, void
    constraints   a limit that refused a command
    gateway       what the gateway reported: acks, status lookups, telemetry
    tool          what an MCP tool returned or raised
"""
import json
import time
import uuid
from pathlib import Path

DEFAULT_TRACE_DIR = Path(__file__).resolve().parent.parent / "traces"
SOURCES = frozenset({"harness", "model", "operator", "auto_approval",
                     "scripted_approval", "approval_gate", "executor",
                     "constraints", "gateway", "tool"})


class Trace:
    def __init__(self, run_id=None, directory=None, task=None, provider=None):
        self.run_id = run_id or f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        directory = Path(directory or DEFAULT_TRACE_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{self.run_id}.jsonl"
        self._started = time.time()
        self._handle = self.path.open("a", encoding="utf-8")
        self.write("run_started", source="harness", task=task,
                   provider=provider)

    def write(self, event, *, source, **fields):
        """Required, not defaulted: a line that does not say whose it is is the
        problem this field exists to fix."""
        record = {
            "run_id": self.run_id,
            "at_unix_ms": int(time.time() * 1000),
            "elapsed_s": round(time.time() - self._started, 3),
            "event": event,
            "source": source,
        }
        record.update(fields)
        self._handle.write(json.dumps(record, default=str) + "\n")
        self._handle.flush()          # a crashed run must still leave its trace

    def close(self, **fields):
        self.write("run_finished", source="harness", **fields)
        self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if not self._handle.closed:
            self._handle.close()


class NullTrace:
    """For tests that care about behaviour rather than evidence."""

    run_id = "run-null"
    path = None

    def write(self, event, *, source, **fields):
        pass

    def close(self, **fields):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass
