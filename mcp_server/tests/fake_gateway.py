"""A gateway that never leaves the process.

A layer-one test should fail for exactly one reason: the code under test is
wrong. A real gateway drags in Docker, SITL, an EKF that needs a minute to
settle, and a vehicle that disarms itself if you dawdle — every one of which can
redden a test that has nothing to do with the harness. This fake speaks the same
protobuf over the same UDP sockets and knows nothing about aircraft.

It is deliberately faithful about the two behaviours the client depends on:
deduplicating on operation_id, and recording an outcome before sending it.
"""
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "vehicle_gateway" / "generated"))
import vehicle_pb2                                             # noqa: E402


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class FakeGateway:
    """Speaks the gateway's wire protocol, with the failure modes on switches.

    drop_first_ack   swallow the first reply for each operation_id, as the real
                     link-fault layer does
    silent           never reply to anything, as a dead gateway does
    reject_with      force every command to this CommandStatus
    """

    def __init__(self, telemetry_port, vehicles=("copter_1", "copter_2"),
                 drop_first_ack=False, silent=False, reject_with=None,
                 telemetry_hz=40.0):
        self.telemetry_port = telemetry_port
        self.vehicles = list(vehicles)
        self.drop_first_ack = drop_first_ack
        self.silent = silent
        self.reject_with = reject_with
        self.telemetry_hz = telemetry_hz
        self.telemetry_enabled = True

        # What a test asserts on.
        self.received_operation_ids = []   # every command datagram seen
        self.executed_operation_ids = []   # the ones that were not replays
        self.executed_commands = []        # the VehicleCommands actually applied
        self.status_queries = []

        self._ledger = {}
        self._state_version = {name: 0 for name in self.vehicles}
        self._dropped_once = set()
        self._running = False
        self._socket = None

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.settimeout(0.2)
        self.command_port = self._socket.getsockname()[1]
        self._running = True
        threading.Thread(target=self._serve, daemon=True).start()
        threading.Thread(target=self._broadcast, daemon=True).start()
        return self

    def stop(self):
        self._running = False
        time.sleep(0.05)
        if self._socket is not None:
            self._socket.close()

    def state_version(self, vehicle_id="copter_1"):
        return self._state_version.get(vehicle_id, 0)

    # -- the protocol -------------------------------------------------------

    def _serve(self):
        while self._running:
            try:
                datagram, sender = self._socket.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            request = vehicle_pb2.ClientRequest()
            try:
                request.ParseFromString(datagram)
            except Exception:
                continue
            kind = request.WhichOneof("request")
            if kind == "command":
                self._handle_command(request.command, sender)
            elif kind == "status_query":
                self._handle_query(request.status_query, sender)

    def _handle_command(self, command, sender):
        operation_id = command.operation_id
        self.received_operation_ids.append(operation_id)

        recorded = self._ledger.get(operation_id)
        if recorded is not None:
            replay = vehicle_pb2.CommandAck()
            replay.CopyFrom(recorded)
            if recorded.status == vehicle_pb2.ACCEPTED:
                replay.status = vehicle_pb2.ALREADY_APPLIED
                replay.reason = "duplicate operation_id, not reapplied"
            self._reply_ack(replay, sender, droppable=True)
            return

        self.executed_operation_ids.append(operation_id)
        self.executed_commands.append(command)
        vehicle_id = command.vehicle_id
        status = self.reject_with or vehicle_pb2.ACCEPTED
        if status == vehicle_pb2.ACCEPTED:
            self._state_version[vehicle_id] = self._state_version.get(vehicle_id, 0) + 1

        ack = vehicle_pb2.CommandAck(
            operation_id=operation_id,
            vehicle_id=vehicle_id,
            status=status,
            reason="fake gateway",
            state_version=self._state_version.get(vehicle_id, 0),
            acked_at_unix_ms=int(time.time() * 1000),
        )
        # Recorded before sending, exactly as the real gateway does: a test for
        # reconciliation is meaningless if the record depends on the send.
        self._ledger[operation_id] = ack
        self._reply_ack(ack, sender, droppable=True)

    def _handle_query(self, query, sender):
        self.status_queries.append(query.operation_id)
        if self.silent:
            return
        recorded = self._ledger.get(query.operation_id)
        response = vehicle_pb2.GatewayResponse()
        response.status.operation_id = query.operation_id
        response.status.is_known = recorded is not None
        if recorded is not None:
            response.status.ack.CopyFrom(recorded)
        self._socket.sendto(response.SerializeToString(), sender)

    def _reply_ack(self, ack, sender, droppable):
        if self.silent:
            return
        if (droppable and self.drop_first_ack
                and ack.operation_id not in self._dropped_once):
            self._dropped_once.add(ack.operation_id)
            return
        response = vehicle_pb2.GatewayResponse()
        response.ack.CopyFrom(ack)
        self._socket.sendto(response.SerializeToString(), sender)

    # -- telemetry ----------------------------------------------------------

    def _broadcast(self):
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        interval = 1.0 / self.telemetry_hz
        while self._running:
            if self.telemetry_enabled and not self.silent:
                for vehicle_id in self.vehicles:
                    sample = vehicle_pb2.VehicleTelemetry(
                        vehicle_id=vehicle_id,
                        sampled_at_unix_ms=int(time.time() * 1000),
                        latitude_deg=51.501592,
                        longitude_deg=-2.551791,
                        altitude_m=0.0,
                        flight_mode="GUIDED",
                        is_armed=False,
                        gps_fix_type=6,
                        battery_percent=100.0,
                        state_version=self._state_version.get(vehicle_id, 0),
                    )
                    try:
                        sender.sendto(sample.SerializeToString(),
                                      ("127.0.0.1", self.telemetry_port))
                    except OSError:
                        pass
            time.sleep(interval)
        sender.close()
