"""One JSONL line per thing that happened.

The model's closing sentence is not evidence. When a run goes wrong the question
is always "what did it actually call, with what, and what came back" — and the
answer has to survive the process. Each line is self-contained so the file can
be tailed, grepped, replayed, or fed to Sunday's MCAP writer and SFT export
without parsing state across lines.
"""
import json
import time
import uuid
from pathlib import Path

DEFAULT_TRACE_DIR = Path(__file__).resolve().parent.parent / "traces"


class Trace:
    def __init__(self, run_id=None, directory=None, task=None, provider=None):
        self.run_id = run_id or f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        directory = Path(directory or DEFAULT_TRACE_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{self.run_id}.jsonl"
        self._started = time.time()
        self._handle = self.path.open("a", encoding="utf-8")
        self.write("run_started", task=task, provider=provider)

    def write(self, event, **fields):
        record = {
            "run_id": self.run_id,
            "at_unix_ms": int(time.time() * 1000),
            "elapsed_s": round(time.time() - self._started, 3),
            "event": event,
        }
        record.update(fields)
        self._handle.write(json.dumps(record, default=str) + "\n")
        self._handle.flush()          # a crashed run must still leave its trace

    def close(self, **fields):
        self.write("run_finished", **fields)
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

    def write(self, event, **fields):
        pass

    def close(self, **fields):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass
