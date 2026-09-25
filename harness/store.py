"""The harness's own durable memory: what it meant to do, and what became of it.

The gateway keeps a ledger too, but that one is in memory and belongs to the
vehicle side. This one is ours, it is on disk, and it exists so that two
questions survive a crash:

  What did I approve?   An approval is bound to exact arguments, a telemetry
                        version, an expiry and the op_id it will use. Anything
                        else executing under that approval is a different
                        command wearing its name.

  What did I send?      A command is written here BEFORE the datagram leaves.
                        A record that only appears after a successful send is
                        useless in exactly the case it is needed for.

Lifecycle:

    proposed -> awaiting_approval -> submitted -> applied
                              |           |----> failed
                              |           |----> unknown
                              |--> rejected
                              |--> expired
"""
import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "mission_store.sqlite3"

PROPOSED = "proposed"
AWAITING_APPROVAL = "awaiting_approval"
SUBMITTED = "submitted"
APPLIED = "applied"
FAILED = "failed"
UNKNOWN = "unknown"
REJECTED = "rejected"
EXPIRED = "expired"

TERMINAL_STATES = {APPLIED, FAILED, REJECTED, EXPIRED}

APPROVAL_PENDING = "pending"
APPROVAL_GRANTED = "granted"
APPROVAL_REFUSED = "refused"


def canonical_args(args):
    """One spelling per set of arguments, so 'the same command' is decidable.

    A model may emit 15 where it emitted 15.0 before, or order keys differently.
    Neither is a different command, and an approval that broke on either would
    be useless. Numbers become floats at a fixed precision; keys are sorted.
    """
    def normalise(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return round(float(value), 6)
        if isinstance(value, dict):
            return {k: normalise(v) for k, v in sorted(value.items())}
        if isinstance(value, list):
            return [normalise(v) for v in value]
        return value

    return json.dumps(normalise(args or {}), sort_keys=True, separators=(",", ":"))


def args_fingerprint(args):
    return hashlib.sha256(canonical_args(args).encode("utf-8")).hexdigest()[:16]


def now_ms():
    return int(time.time() * 1000)


@dataclass
class CommandRecord:
    op_id: str
    run_id: str
    vehicle_id: str
    tool: str
    args: dict
    args_hash: str
    state: str
    proposed_state_version: int = 0
    result_state_version: int = 0
    reason: str = ""
    approval_id: str = ""
    created_at_ms: int = 0
    updated_at_ms: int = 0

    @property
    def is_terminal(self):
        return self.state in TERMINAL_STATES


@dataclass
class ApprovalRecord:
    approval_id: str
    run_id: str
    op_id: str
    vehicle_id: str
    tool: str
    args: dict
    args_hash: str
    proposed_state_version: int
    expires_at_ms: int
    decision: str = APPROVAL_PENDING
    decided_at_ms: int = 0
    note: str = ""

    def expired(self, at_ms=None):
        return (at_ms or now_ms()) > self.expires_at_ms

    def covers(self, tool, args, state_version, at_ms=None):
        """Does this approval authorise exactly this command, right now?

        Four ways it does not: the wrong tool, arguments that differ in any
        respect, a world that has moved on since it was granted, or simply too
        much time having passed. All four are the same failure — permission was
        given for something that is no longer what is about to happen.
        """
        if self.decision != APPROVAL_GRANTED:
            return False, f"approval is {self.decision}, not granted"
        if self.expired(at_ms):
            return False, "approval has expired"
        if tool != self.tool:
            return False, f"approval was for {self.tool}, not {tool}"
        if args_fingerprint(args) != self.args_hash:
            return False, ("arguments differ from the ones approved: approved "
                           f"{canonical_args(self.args)}, now "
                           f"{canonical_args(args)}")
        if state_version != self.proposed_state_version:
            return False, (f"vehicle was at state_version "
                           f"{self.proposed_state_version} when this was "
                           f"approved and is now at {state_version}")
        return True, "approval covers this command"


SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
    op_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    proposed_state_version INTEGER DEFAULT 0,
    result_state_version INTEGER DEFAULT 0,
    reason TEXT DEFAULT '',
    approval_id TEXT DEFAULT '',
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    op_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    proposed_state_version INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    decision TEXT NOT NULL,
    decided_at_ms INTEGER DEFAULT 0,
    note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS commands_by_state ON commands(state);
CREATE INDEX IF NOT EXISTS commands_by_run ON commands(run_id);
"""


class MissionStore:

    def __init__(self, path=None):
        self.path = str(path or DEFAULT_DB)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # -- commands -----------------------------------------------------------

    def propose(self, run_id, vehicle_id, tool, args, state_version=0):
        """Record an intention, and mint the op_id it will carry if approved.

        The id is allocated here rather than at send time because the approval
        binds to it: the operator approves one specific command, not a class of
        them, and a retry of that command must reuse the same id.
        """
        record = CommandRecord(
            op_id=f"op-{uuid.uuid4().hex[:12]}",
            run_id=run_id, vehicle_id=vehicle_id, tool=tool,
            args=dict(args or {}), args_hash=args_fingerprint(args),
            state=PROPOSED, proposed_state_version=state_version,
            created_at_ms=now_ms(), updated_at_ms=now_ms())
        self._db.execute(
            "INSERT INTO commands (op_id, run_id, vehicle_id, tool, args, "
            "args_hash, state, proposed_state_version, created_at_ms, "
            "updated_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (record.op_id, run_id, vehicle_id, tool, json.dumps(record.args),
             record.args_hash, PROPOSED, state_version,
             record.created_at_ms, record.updated_at_ms))
        self._db.commit()
        return record

    def set_state(self, op_id, state, reason="", result_state_version=None,
                  approval_id=None):
        fields = ["state = ?", "updated_at_ms = ?"]
        values = [state, now_ms()]
        if reason:
            fields.append("reason = ?")
            values.append(reason)
        if result_state_version is not None:
            fields.append("result_state_version = ?")
            values.append(result_state_version)
        if approval_id is not None:
            fields.append("approval_id = ?")
            values.append(approval_id)
        values.append(op_id)
        self._db.execute(
            f"UPDATE commands SET {', '.join(fields)} WHERE op_id = ?", values)
        self._db.commit()
        return self.command(op_id)

    def command(self, op_id):
        row = self._db.execute(
            "SELECT * FROM commands WHERE op_id = ?", (op_id,)).fetchone()
        return self._to_command(row) if row else None

    def commands_for_run(self, run_id):
        rows = self._db.execute(
            "SELECT * FROM commands WHERE run_id = ? ORDER BY created_at_ms",
            (run_id,)).fetchall()
        return [self._to_command(row) for row in rows]

    def outstanding(self):
        """Commands that were sent but never resolved.

        This is the list a restarting harness must reconcile before it does
        anything else: each one may or may not have reached the vehicle.
        """
        rows = self._db.execute(
            "SELECT * FROM commands WHERE state IN (?, ?) ORDER BY created_at_ms",
            (SUBMITTED, UNKNOWN)).fetchall()
        return [self._to_command(row) for row in rows]

    @staticmethod
    def _to_command(row):
        return CommandRecord(
            op_id=row["op_id"], run_id=row["run_id"],
            vehicle_id=row["vehicle_id"], tool=row["tool"],
            args=json.loads(row["args"]), args_hash=row["args_hash"],
            state=row["state"],
            proposed_state_version=row["proposed_state_version"],
            result_state_version=row["result_state_version"],
            reason=row["reason"], approval_id=row["approval_id"],
            created_at_ms=row["created_at_ms"],
            updated_at_ms=row["updated_at_ms"])

    # -- approvals ----------------------------------------------------------

    def request_approval(self, command, ttl_s=120):
        record = ApprovalRecord(
            approval_id=f"apr-{uuid.uuid4().hex[:10]}",
            run_id=command.run_id, op_id=command.op_id,
            vehicle_id=command.vehicle_id, tool=command.tool,
            args=dict(command.args), args_hash=command.args_hash,
            proposed_state_version=command.proposed_state_version,
            expires_at_ms=now_ms() + int(ttl_s * 1000))
        self._db.execute(
            "INSERT INTO approvals (approval_id, run_id, op_id, vehicle_id, "
            "tool, args, args_hash, proposed_state_version, expires_at_ms, "
            "decision) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (record.approval_id, record.run_id, record.op_id, record.vehicle_id,
             record.tool, json.dumps(record.args), record.args_hash,
             record.proposed_state_version, record.expires_at_ms,
             APPROVAL_PENDING))
        self._db.commit()
        self.set_state(command.op_id, AWAITING_APPROVAL,
                       approval_id=record.approval_id)
        return record

    def decide(self, approval_id, granted, note=""):
        self._db.execute(
            "UPDATE approvals SET decision = ?, decided_at_ms = ?, note = ? "
            "WHERE approval_id = ?",
            (APPROVAL_GRANTED if granted else APPROVAL_REFUSED, now_ms(),
             note, approval_id))
        self._db.commit()
        return self.approval(approval_id)

    def approval(self, approval_id):
        row = self._db.execute(
            "SELECT * FROM approvals WHERE approval_id = ?",
            (approval_id,)).fetchone()
        if row is None:
            return None
        return ApprovalRecord(
            approval_id=row["approval_id"], run_id=row["run_id"],
            op_id=row["op_id"], vehicle_id=row["vehicle_id"],
            tool=row["tool"], args=json.loads(row["args"]),
            args_hash=row["args_hash"],
            proposed_state_version=row["proposed_state_version"],
            expires_at_ms=row["expires_at_ms"], decision=row["decision"],
            decided_at_ms=row["decided_at_ms"], note=row["note"])
