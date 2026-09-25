#!/usr/bin/env python3
"""Client side of the vehicle gateway: the half that has to cope.

The gateway is allowed to be unreachable, slow, or to have its replies eaten by
the link. Everything in this file exists so that callers above it -- the MCP
tool layer, the harness, the tests -- never have to guess what happened.

Three ideas carry the weight:

  operation_id is ours.  We generate it, we keep it stable across retries, and
  the gateway deduplicates on it. That makes a retry free, which is what lets
  us retry at all.

  Silence is not failure.  A write that gets no ack has an UNKNOWN outcome, and
  unknown is a state we report, not a coin we flip. Before reporting it we ask
  the gateway what it recorded.

  Stale is not fresh.  Telemetry has an age. Past a threshold we say the vehicle
  is unreachable rather than hand back the last thing we happened to hear.
"""
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "generated"))
import vehicle_pb2                                             # noqa: E402

DEFAULT_ACK_TIMEOUT_S = 6.0
# 5 Hz means 25 missed samples. Generous on purpose: the simulator stack stalls
# for ~3.3 s every ~34 s (measured), and a limit below that turns a transport
# hiccup into "the vehicle is unreachable" in the middle of a healthy flight.
DEFAULT_TELEMETRY_MAX_AGE_S = 5.0

# The gateway's five-value CommandStatus collapsed into the three outcomes a
# caller actually branches on, plus whether waiting could change the answer.
_GATEWAY_STATUS_MAP = {
    vehicle_pb2.ACCEPTED:            ("accepted", False),
    vehicle_pb2.ALREADY_APPLIED:     ("accepted", False),
    vehicle_pb2.REJECTED_RETRYABLE:  ("rejected", True),
    vehicle_pb2.REJECTED_PERMANENT:  ("rejected", False),
    vehicle_pb2.STALE_STATE_VERSION: ("rejected", True),
}


class GatewayUnavailable(Exception):
    """The gateway did not answer at all. Distinct from 'it said no'."""


@dataclass
class CommandOutcome:
    operation_id: str
    status: str                  # "accepted" | "rejected" | "unknown"
    reason: str
    retryable: bool = False
    state_version: int = 0
    gateway_status: str = ""     # the un-collapsed enum name, for traces
    reconciled: bool = False     # true if we had to ask rather than be told

    def as_dict(self):
        return {
            "op_id": self.operation_id,
            "status": self.status,
            "reason": self.reason,
            "retryable": self.retryable,
            "state_version": self.state_version,
            "gateway_status": self.gateway_status,
            "reconciled": self.reconciled,
        }


def telemetry_as_dict(telemetry, age_s):
    """Protobuf to plain JSON-able fields, with units in the names."""
    return {
        "vehicle_id": telemetry.vehicle_id,
        "latitude_deg": round(telemetry.latitude_deg, 7),
        "longitude_deg": round(telemetry.longitude_deg, 7),
        "altitude_m": round(telemetry.altitude_m, 2),
        "ground_speed_mps": round(telemetry.ground_speed_mps, 2),
        "heading_deg": round(telemetry.heading_deg, 1),
        "flight_mode": telemetry.flight_mode,
        "is_armed": telemetry.is_armed,
        "gps_fix_type": telemetry.gps_fix_type,
        "battery_percent": round(telemetry.battery_percent, 1),
        "state_version": telemetry.state_version,
        "age_s": round(age_s, 2),
    }


class GatewayClient:
    """One UDP conversation with the gateway, plus a telemetry subscription.

    Only one process can usefully bind the telemetry port at a time: the gateway
    sends to a single unicast address, so a second listener steals datagrams
    rather than sharing them. Run the MCP server or `probe.py telemetry`, not
    both.
    """

    def __init__(self, host="127.0.0.1", command_port=14550,
                 telemetry_port=14551, ack_timeout_s=DEFAULT_ACK_TIMEOUT_S):
        self.host = host
        self.command_port = command_port
        self.telemetry_port = telemetry_port
        self.ack_timeout_s = ack_timeout_s

        self._command_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._command_socket.settimeout(ack_timeout_s)
        self._command_lock = threading.Lock()

        self._telemetry = {}          # vehicle_id -> (VehicleTelemetry, at)
        self._telemetry_lock = threading.Lock()
        self._telemetry_socket = None
        self._running = False
        # A reader that arrives before the first 5 Hz sample would otherwise
        # conclude the vehicle is missing when it is merely early.
        self._first_sample = threading.Event()

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        self._telemetry_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._telemetry_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._telemetry_socket.bind(("0.0.0.0", self.telemetry_port))
        self._telemetry_socket.settimeout(0.5)
        self._running = True
        threading.Thread(target=self._listen, daemon=True).start()
        return self

    def close(self):
        self._running = False
        if self._telemetry_socket is not None:
            self._telemetry_socket.close()
        self._command_socket.close()

    def _listen(self):
        while self._running:
            try:
                datagram, _ = self._telemetry_socket.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            sample = vehicle_pb2.VehicleTelemetry()
            try:
                sample.ParseFromString(datagram)
            except Exception:
                continue
            with self._telemetry_lock:
                self._telemetry[sample.vehicle_id] = (sample, time.time())
            self._first_sample.set()

    def await_first_sample(self, timeout_s=1.5):
        """Block briefly for the first telemetry datagram of this process.

        Only the first one: after that a missing sample is real news, not a
        cold start, and waiting would hide exactly the staleness we report.
        """
        return self._first_sample.wait(timeout_s)

    # -- reads --------------------------------------------------------------

    def known_vehicles(self, max_age_s=DEFAULT_TELEMETRY_MAX_AGE_S):
        """Vehicles we have heard from, and whether we still hear them.

        Derived from telemetry rather than asked for: a vehicle that is not
        broadcasting is not a vehicle you can command, whatever a config says.
        """
        with self._telemetry_lock:
            empty = not self._telemetry
        if empty:
            self.await_first_sample()
        now = time.time()
        with self._telemetry_lock:
            items = list(self._telemetry.items())
        return [
            {
                "vehicle_id": vehicle_id,
                "reachable": (now - at) <= max_age_s,
                "last_seen_s_ago": round(now - at, 2),
                "flight_mode": sample.flight_mode,
                "is_armed": sample.is_armed,
                "state_version": sample.state_version,
            }
            for vehicle_id, (sample, at) in sorted(items)
        ]

    def fleet_snapshot(self, max_age_s=DEFAULT_TELEMETRY_MAX_AGE_S):
        """Every vehicle we can currently see, keyed by id.

        A fleet rule needs the fleet. Vehicles that have gone quiet are left
        out rather than included stale: a separation check against a position
        from ten seconds ago is worse than no check, because it reads as one.
        """
        snapshot = {}
        for entry in self.known_vehicles(max_age_s=max_age_s):
            if not entry["reachable"]:
                continue
            try:
                snapshot[entry["vehicle_id"]] = self.telemetry(
                    entry["vehicle_id"], max_age_s=max_age_s)
            except GatewayUnavailable:
                continue
        return snapshot

    def telemetry(self, vehicle_id, max_age_s=DEFAULT_TELEMETRY_MAX_AGE_S):
        """Freshest sample for one vehicle, or raise rather than lie.

        Handing back a stale sample is how an agent ends up reasoning about a
        vehicle that stopped talking ten seconds ago.
        """
        with self._telemetry_lock:
            entry = self._telemetry.get(vehicle_id)
        if entry is None:
            self.await_first_sample()
            with self._telemetry_lock:
                entry = self._telemetry.get(vehicle_id)
        if entry is None:
            seen = sorted(self._telemetry)
            raise GatewayUnavailable(
                f"no telemetry has ever arrived for '{vehicle_id}'; "
                f"vehicles currently broadcasting: {seen or 'none'}. "
                f"Check the gateway is running and reaching udp/"
                f"{self.telemetry_port}.")
        sample, at = entry
        age_s = time.time() - at
        if age_s > max_age_s:
            raise GatewayUnavailable(
                f"telemetry for '{vehicle_id}' is {age_s:.1f}s old "
                f"(limit {max_age_s}s); treat the vehicle as unreachable "
                f"rather than acting on this sample.")
        return telemetry_as_dict(sample, age_s)

    # -- writes -------------------------------------------------------------

    def new_operation_id(self):
        return f"op-{uuid.uuid4().hex[:12]}"

    def send_command(self, command, operation_id=None):
        """Send one command and return a decided outcome, reconciling if needed.

        The retry logic deliberately does NOT loop: one attempt, then one
        status query. Looping belongs to the caller, which knows whether the
        mission still wants this and has the budget to spend.
        """
        operation_id = operation_id or self.new_operation_id()
        command.operation_id = operation_id
        command.issued_at_unix_ms = int(time.time() * 1000)

        request = vehicle_pb2.ClientRequest()
        request.command.CopyFrom(command)

        with self._command_lock:
            try:
                self._command_socket.sendto(
                    request.SerializeToString(), (self.host, self.command_port))
                datagram, _ = self._command_socket.recvfrom(65535)
            except socket.timeout:
                datagram = None
            except OSError as error:
                raise GatewayUnavailable(
                    f"could not reach the gateway at {self.host}:"
                    f"{self.command_port}: {error}") from error

        if datagram is not None:
            response = vehicle_pb2.GatewayResponse()
            response.ParseFromString(datagram)
            return self._outcome_from_ack(response.ack, reconciled=False)

        # No ack. The command may well have applied, so ask before deciding.
        recorded = self.command_status(operation_id, raise_if_silent=False)
        if recorded is None:
            return CommandOutcome(
                operation_id=operation_id,
                status="unknown",
                reason=(f"no ack within {self.ack_timeout_s}s and the gateway "
                        f"did not answer a status query either. The command may "
                        f"or may not have been applied. Read telemetry before "
                        f"acting; retrying with this same op_id is safe."),
                retryable=True,
            )
        if not recorded["is_known"]:
            return CommandOutcome(
                operation_id=operation_id,
                status="unknown",
                reason=("the gateway has no record of this operation, so it "
                        "most likely never arrived. Re-sending this same op_id "
                        "is safe."),
                retryable=True,
                reconciled=True,
            )
        return self._outcome_from_ack(recorded["_ack"], reconciled=True)

    def command_status(self, operation_id, raise_if_silent=True):
        """Ask the gateway what it recorded for one operation_id."""
        query = vehicle_pb2.ClientRequest()
        query.status_query.operation_id = operation_id

        with self._command_lock:
            try:
                self._command_socket.sendto(
                    query.SerializeToString(), (self.host, self.command_port))
                datagram, _ = self._command_socket.recvfrom(65535)
            except socket.timeout:
                if raise_if_silent:
                    raise GatewayUnavailable(
                        f"gateway did not answer a status query for "
                        f"{operation_id} within {self.ack_timeout_s}s")
                return None
            except OSError as error:
                raise GatewayUnavailable(str(error)) from error

        response = vehicle_pb2.GatewayResponse()
        response.ParseFromString(datagram)
        status = response.status
        result = {"op_id": operation_id, "is_known": status.is_known}
        if status.is_known:
            outcome = self._outcome_from_ack(status.ack, reconciled=True)
            result.update(outcome.as_dict())
            result["_ack"] = status.ack
        return result

    def _outcome_from_ack(self, ack, reconciled):
        status, retryable = _GATEWAY_STATUS_MAP.get(
            ack.status, ("unknown", True))
        return CommandOutcome(
            operation_id=ack.operation_id,
            status=status,
            reason=ack.reason,
            retryable=retryable,
            state_version=ack.state_version,
            gateway_status=vehicle_pb2.CommandStatus.Name(ack.status),
            reconciled=reconciled,
        )

    # -- command builders ---------------------------------------------------

    @staticmethod
    def build(vehicle_id, action, **fields):
        """One place that knows how a protobuf oneof is filled in."""
        command = vehicle_pb2.VehicleCommand(vehicle_id=vehicle_id)
        if action in ("arm", "disarm", "return_to_launch", "land"):
            getattr(command, action).SetInParent()
        elif action == "set_mode":
            command.set_mode.mode_name = fields["mode_name"]
        elif action == "takeoff":
            command.takeoff.target_altitude_m = fields["target_altitude_m"]
        elif action == "goto_position":
            command.goto_position.target_latitude_deg = fields["latitude_deg"]
            command.goto_position.target_longitude_deg = fields["longitude_deg"]
            command.goto_position.target_altitude_m = fields["altitude_m"]
        else:
            raise ValueError(f"unknown action '{action}'")
        if fields.get("expected_state_version"):
            command.expected_state_version = fields["expected_state_version"]
        return command
