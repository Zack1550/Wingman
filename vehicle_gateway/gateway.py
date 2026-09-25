#!/usr/bin/env python3
"""UDP/protobuf vehicle gateway in front of ArduPilot SITL.

The harness never speaks MAVLink. It sends protobuf commands to this process
over UDP and listens to a protobuf telemetry broadcast. Everything
MAVLink-shaped stops here: 1e7-scaled coordinates, position type masks,
MAV_CMD numbers, the fact that "go here" is a setpoint rather than a command,
and the fact that ArduPilot sends no telemetry until a ground station asks.

One rule runs through the whole file: an Ack means the vehicle ACCEPTED a
command, never that the manoeuvre finished. Telemetry is the only ground truth
for what the vehicle actually did. That distinction is what makes a dropped ack
survivable instead of a guess.

    harness  --UDP 14550-->  [command port]  \
    harness  <--UDP 14550--  [acks]           >--MAVLink TCP 5760--> SITL
    harness  <--UDP 14551--  [telemetry 5Hz] /

Run it:
    python gateway.py                    # an honest link
    python gateway.py --drop-first-ack   # the vehicle moves, the client is blind
"""
import argparse
import random
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from pymavlink import mavutil

sys.path.insert(0, str(Path(__file__).parent / "generated"))
import vehicle_pb2                                             # noqa: E402

# --- constants -------------------------------------------------------------

DEGREES_TO_MAVLINK_INT = 1e7        # MAVLink carries lat/lon as scaled ints
# These are not round numbers. This stack — WSL2, Docker, ArduPilot SITL —
# stalls for about 3.3 s roughly every 34 s, measured with a bare pymavlink
# listener and no gateway running, so it is the environment and not this code.
# Every timeout below must exceed that floor, or it fires on a hiccup and
# reports a vehicle problem that does not exist. Re-measure on other hardware.
TRANSPORT_WORST_GAP_S = 3.3
MAVLINK_ACK_TIMEOUT_S = 5.0         # a vehicle that hasn't answered by now won't
MODE_CHANGE_TIMEOUT_S = 8.0         # confirmed by heartbeat, not by an ack
ARM_CONFIRM_TIMEOUT_S = 8.0         # same again: heartbeats are only 1 Hz
TELEMETRY_HZ = 5.0
HEARTBEAT_HZ = 1.0                  # what every MAVLink endpoint owes the network
STALE_COMMAND_AGE_MS = 30_000       # older than this and the world has moved on
# If MAVLink has said nothing about a vehicle for this long, we do not know what
# it is doing and must stop saying that we do. Comfortably above the measured
# transport stall, so a hiccup is not reported as a missing aircraft.
TELEMETRY_STALE_AFTER_S = 6.0
# Above this the aircraft is flying, and NAV_TAKEOFF is no longer the command
# anyone means. ArduPilot refuses it but does not always say why.
AIRBORNE_ALTITUDE_M = 1.0
IGNORE_ALL_BUT_POSITION = 0b0000111111111000

MAV_RESULT_NAMES = {
    0: "ACCEPTED", 1: "TEMPORARILY_REJECTED", 2: "DENIED",
    3: "UNSUPPORTED", 4: "FAILED", 5: "IN_PROGRESS",
}

# How the vehicle's answer becomes the client's answer. The split that matters
# is retryable versus permanent: it tells the model whether waiting helps.
MAV_RESULT_TO_STATUS = {
    0: vehicle_pb2.ACCEPTED,
    5: vehicle_pb2.ACCEPTED,            # IN_PROGRESS: taken, still running
    1: vehicle_pb2.REJECTED_RETRYABLE,  # pre-arm checks, EKF still settling
    4: vehicle_pb2.REJECTED_RETRYABLE,
    2: vehicle_pb2.REJECTED_PERMANENT,  # DENIED: retrying changes nothing
    3: vehicle_pb2.REJECTED_PERMANENT,
}

ACM_MODE_NUMBERS = {name: number for number, name in mavutil.mode_mapping_acm.items()}

# The handful a mission planner actually reaches for, named in rejections.
COMMON_MODES = ("GUIDED", "LOITER", "RTL", "LAND", "ALT_HOLD", "STABILIZE", "AUTO")

# ArduCopter refuses to arm in modes that are already flying the aircraft.
NON_ARMABLE_MODES = {"RTL", "LAND", "AUTO", "AUTO_RTL", "SMART_RTL", "BRAKE",
                     "THROW", "FLIP"}
STATUS_RELEVANT_FOR_S = 6.0        # how far back a rejection looks for a reason


def now_unix_ms():
    return int(time.time() * 1000)


def log(*parts):
    print(f"[{time.strftime('%H:%M:%S')}]", *parts, flush=True)


# --- per-vehicle state -----------------------------------------------------

@dataclass
class VehicleState:
    """Everything the gateway knows about one copter.

    Written only by the MAVLink reader thread, read by everyone, all of it
    under `lock`. state_version increments on every accepted command and is the
    hook an approval or a stale write hangs off: a client that approved an
    action against version 7 can refuse to apply it at version 9.
    """
    vehicle_id: str
    system_id: int
    lock: threading.Lock = field(default_factory=threading.Lock)

    heard_from: bool = False
    last_message_at: float = 0.0      # wall clock of the last MAVLink message
    latitude_deg: float = 0.0
    longitude_deg: float = 0.0
    altitude_m: float = 0.0             # above home, not above sea level
    ground_speed_mps: float = 0.0
    heading_deg: float = 0.0
    flight_mode: str = "?"
    is_armed: bool = False
    gps_fix_type: int = 0
    battery_percent: float = 0.0
    state_version: int = 0
    sampled_at_unix_ms: int = 0
    battery_override: float = None     # eval fixture; None means report truth
    # The aircraft explains its own refusals in STATUSTEXT ("Arm: Need Position
    # Estimate"). Logging those to a console the model cannot read, and then
    # answering it with "FAILED", throws away the only useful part.
    recent_status: list = field(default_factory=list)   # (unix_s, text)

    def snapshot(self):
        """A telemetry message for whatever this vehicle looks like right now."""
        with self.lock:
            return vehicle_pb2.VehicleTelemetry(
                vehicle_id=self.vehicle_id,
                sampled_at_unix_ms=self.sampled_at_unix_ms or now_unix_ms(),
                latitude_deg=self.latitude_deg,
                longitude_deg=self.longitude_deg,
                altitude_m=self.altitude_m,
                ground_speed_mps=self.ground_speed_mps,
                heading_deg=self.heading_deg,
                flight_mode=self.flight_mode,
                is_armed=self.is_armed,
                gps_fix_type=self.gps_fix_type,
                battery_percent=(self.battery_percent
                                 if self.battery_override is None
                                 else self.battery_override),
                state_version=self.state_version,
            )

    def note_status(self, text):
        with self.lock:
            self.recent_status.append((time.time(), text))
            del self.recent_status[:-10]

    def recent_status_text(self, within_s=STATUS_RELEVANT_FOR_S):
        cutoff = time.time() - within_s
        with self.lock:
            recent = [text for at, text in self.recent_status if at >= cutoff]
        # Most recent first, de-duplicated: ArduPilot repeats itself.
        seen, ordered = set(), []
        for text in reversed(recent):
            if text not in seen:
                seen.add(text)
                ordered.append(text)
        return "; ".join(ordered[:3])

    def bump_state_version(self):
        with self.lock:
            self.state_version += 1
            return self.state_version


# --- the unreliable link ---------------------------------------------------

class LyingLink:
    """Every outbound datagram passes through here, and here is where it lies.

    A harness that has only ever run over loopback has never been tested. Real
    links drop, duplicate, reorder and delay, and each of those breaks a
    different assumption: a drop makes the outcome unknown, a duplicate makes
    the receiver prove it deduplicates, a reorder makes stale data arrive after
    fresh, a delay makes a timeout fire on a command that was fine.
    """

    def __init__(self, sock, *, drop_first_ack=False, drop_rate=0.0,
                 duplicate_rate=0.0, reorder_rate=0.0, delay_ms=0, seed=None):
        self.socket = sock
        self._drop_first_ack = drop_first_ack
        self._drop_rate = drop_rate
        self._duplicate_rate = duplicate_rate
        self._reorder_rate = reorder_rate
        self._delay_s = delay_ms / 1000.0
        self._random = random.Random(seed)
        self._lock = threading.Lock()
        self._operations_already_dropped = set()
        self._held_datagram = None          # the one being reordered

    def send(self, payload, address, *, operation_id=None, label=""):
        with self._lock:
            # Deterministic, once per operation: the drill the ledger is for.
            if (operation_id is not None and self._drop_first_ack
                    and operation_id not in self._operations_already_dropped):
                self._operations_already_dropped.add(operation_id)
                log(f"  LINK dropped first ack for {operation_id} "
                    f"(vehicle acted anyway)")
                return

            if self._random.random() < self._drop_rate:
                log(f"  LINK dropped {label or 'datagram'}")
                return

            # Reordering needs two datagrams: hold one back, then release it
            # behind the next one so the client sees them swapped.
            if self._held_datagram is not None:
                queued = [(payload, address), self._held_datagram]
                self._held_datagram = None
                log("  LINK released a reordered datagram")
            elif self._random.random() < self._reorder_rate:
                self._held_datagram = (payload, address)
                log(f"  LINK holding {label or 'datagram'} back to reorder it")
                return
            else:
                queued = [(payload, address)]

            for datagram, destination in queued:
                copies = 2 if self._random.random() < self._duplicate_rate else 1
                if copies == 2:
                    log(f"  LINK duplicated {label or 'datagram'}")
                for _ in range(copies):
                    self._transmit(datagram, destination)

    def _transmit(self, payload, address):
        if self._delay_s > 0:
            threading.Timer(self._delay_s, self._sendto,
                            args=(payload, address)).start()
        else:
            self._sendto(payload, address)

    def _sendto(self, payload, address):
        try:
            self.socket.sendto(payload, address)
        except OSError as error:
            log(f"  LINK send failed: {error}")


# --- the command ledger ----------------------------------------------------

class CommandLedger:
    """What the gateway has been asked to do, keyed by operation_id.

    This is the vehicle-side half of idempotency. The client generates an
    operation_id and keeps it stable across retries; the gateway remembers the
    answer it already gave and replays it instead of acting twice. A retry
    after a dropped ack must move the vehicle exactly zero additional times.

    In memory, so a gateway restart forgets. A real vehicle would need this in
    non-volatile storage with a bounded horizon; say so out loud rather than
    pretending loopback proved otherwise.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._acks_by_operation = {}

    def remember(self, ack):
        with self._lock:
            self._acks_by_operation[ack.operation_id] = ack

    def lookup(self, operation_id):
        with self._lock:
            return self._acks_by_operation.get(operation_id)

    def replay(self, original_ack):
        """The answer to a duplicate: the original outcome, marked as a repeat.

        A duplicate of an accepted command reports ALREADY_APPLIED so the client
        can tell "it worked" from "it worked, twice asked". A duplicate of a
        rejection replays the rejection verbatim, because the reason is what the
        model needs and ALREADY_APPLIED would be a lie.
        """
        replayed = vehicle_pb2.CommandAck()
        replayed.CopyFrom(original_ack)
        replayed.acked_at_unix_ms = now_unix_ms()
        if original_ack.status == vehicle_pb2.ACCEPTED:
            replayed.status = vehicle_pb2.ALREADY_APPLIED
            replayed.reason = (f"duplicate operation_id; applied at "
                               f"{original_ack.acked_at_unix_ms}, not reapplied")
        return replayed


# --- MAVLink side ----------------------------------------------------------

class MavlinkBridge:
    """One connection to the router, one reader thread, N vehicles behind it.

    mavlink-router multiplexes every copter onto a single TCP port and tells
    them apart by system id, so the gateway keeps one socket and demultiplexes
    on srcSystem. Sends are serialised with a lock; reads happen only here.
    """

    def __init__(self, connection_string, vehicles):
        self.vehicles_by_id = vehicles
        self.vehicles_by_system = {v.system_id: v for v in vehicles.values()}
        self._send_lock = threading.Lock()
        self._pending_acks = {}
        self._pending_lock = threading.Lock()
        self._running = True

        log(f"connecting to {connection_string} ...")
        self.link = mavutil.mavlink_connection(connection_string)
        self.link.wait_heartbeat()
        log(f"heartbeat from system {self.link.target_system}")

        # ArduPilot streams nothing until a ground station asks, so a gateway
        # that skips this sees no position and reports a vehicle that is fine
        # as missing.
        for system_id in self.vehicles_by_system:
            self.link.mav.request_data_stream_send(
                system_id, 1, mavutil.mavlink.MAV_DATA_STREAM_ALL, 5, 1)

        threading.Thread(target=self._read_forever, daemon=True).start()
        threading.Thread(target=self._heartbeat_forever, daemon=True).start()

    def stop(self):
        self._running = False

    def _heartbeat_forever(self):
        """Announce that we are still here, once a second.

        MAVLink endpoints prune peers that go quiet. This gateway mostly
        listens and only transmits when commanding, so a mission with any pause
        in it looked dead to the router, which closed the TCP connection —
        every 33 seconds, exactly. The symptom was not an error: it was
        telemetry going stale mid-flight and set_mode failing to confirm inside
        its window, which reads like a vehicle problem and is not one.
        """
        interval = 1.0 / HEARTBEAT_HZ
        while self._running:
            try:
                with self._send_lock:
                    self.link.mav.heartbeat_send(
                        mavutil.mavlink.MAV_TYPE_GCS,
                        mavutil.mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0)
            except Exception:
                pass          # the reader thread owns reporting a dead link
            time.sleep(interval)

    # -- reader thread ------------------------------------------------------

    def _read_forever(self):
        last_message_at = time.time()
        complained = False
        while self._running:
            message = self.link.recv_match(blocking=True, timeout=0.5)
            if message is None:
                # recv_match returns None both for an idle link and for one
                # whose far end has gone away. Only the clock tells them apart.
                if (not complained
                        and time.time() - last_message_at > TELEMETRY_STALE_AFTER_S):
                    complained = True
                    log("  LINK DOWN: no MAVLink of any kind. The router or "
                        "the simulator has gone away; restart the gateway "
                        "once it is back.")
                continue
            last_message_at = time.time()
            complained = False
            vehicle = self.vehicles_by_system.get(message.get_srcSystem())
            if vehicle is None:
                continue                    # another copter on the same router
            self._apply(vehicle, message)

    def _apply(self, vehicle, message):
        message_type = message.get_type()

        if message_type == 'COMMAND_ACK':
            self._deliver_ack(vehicle.system_id, message.command, message.result)
            return
        if message_type == 'STATUSTEXT':
            log(f"  [{vehicle.vehicle_id} says] {message.text}")
            vehicle.note_status(message.text)
            return

        with vehicle.lock:
            vehicle.heard_from = True
            vehicle.last_message_at = time.time()
            vehicle.sampled_at_unix_ms = now_unix_ms()
            if message_type == 'GLOBAL_POSITION_INT':
                vehicle.latitude_deg = message.lat / DEGREES_TO_MAVLINK_INT
                vehicle.longitude_deg = message.lon / DEGREES_TO_MAVLINK_INT
                vehicle.altitude_m = message.relative_alt / 1000.0
                vehicle.heading_deg = message.hdg / 100.0
            elif message_type == 'VFR_HUD':
                vehicle.ground_speed_mps = message.groundspeed
            elif message_type == 'GPS_RAW_INT':
                vehicle.gps_fix_type = message.fix_type
            elif message_type == 'SYS_STATUS':
                if message.battery_remaining >= 0:
                    vehicle.battery_percent = float(message.battery_remaining)
            elif message_type == 'HEARTBEAT':
                vehicle.flight_mode = mavutil.mode_string_v10(message)
                vehicle.is_armed = bool(
                    message.base_mode
                    & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)

    # -- waiting for a COMMAND_ACK across threads ---------------------------

    def _deliver_ack(self, system_id, command_id, result):
        with self._pending_lock:
            slot = self._pending_acks.get((system_id, command_id))
        if slot is not None:
            slot["result"] = result
            slot["event"].set()

    def send_command_long(self, system_id, command_id, *params):
        """Send a COMMAND_LONG and wait for the vehicle's own answer.

        Returns a MAV_RESULT code, or None if nothing came back in time. None is
        the interesting case: the command may well have been applied, and the
        only honest thing the gateway can say is "ask telemetry".
        """
        slot = {"event": threading.Event(), "result": None}
        with self._pending_lock:
            self._pending_acks[(system_id, command_id)] = slot

        padded = list(params) + [0.0] * (7 - len(params))
        with self._send_lock:
            self.link.mav.command_long_send(system_id, 1, command_id, 0, *padded)

        slot["event"].wait(MAVLINK_ACK_TIMEOUT_S)
        with self._pending_lock:
            self._pending_acks.pop((system_id, command_id), None)
        return slot["result"]

    def send_mode_change(self, vehicle, mode_name):
        """Ask for a flight mode and confirm it by heartbeat.

        ArduPilot does not COMMAND_ACK a SET_MODE, so "did it work" is answered
        by watching the mode actually change. Same shape as the whole gateway:
        the readback is the truth, not the request.
        """
        mode_number = ACM_MODE_NUMBERS.get(mode_name.upper())
        if mode_number is None:
            return False, f"unknown flight mode '{mode_name}'"

        with self._send_lock:
            self.link.mav.set_mode_send(
                vehicle.system_id,
                mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                mode_number)

        deadline = time.time() + MODE_CHANGE_TIMEOUT_S
        while time.time() < deadline:
            with vehicle.lock:
                if vehicle.flight_mode == mode_name.upper():
                    return True, f"mode is {mode_name.upper()}"
            time.sleep(0.1)
        with vehicle.lock:
            return False, (f"still in {vehicle.flight_mode} after "
                           f"{MODE_CHANGE_TIMEOUT_S:.0f}s")

    def await_armed_state(self, vehicle, armed, timeout_s=ARM_CONFIRM_TIMEOUT_S):
        """Wait for HEARTBEAT to report the armed state we asked for."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            with vehicle.lock:
                if vehicle.is_armed == armed:
                    return True
            time.sleep(0.05)
        return False

    def send_position_target(self, vehicle, latitude_deg, longitude_deg, altitude_m):
        """A goto is a setpoint, not a command: no ack exists to wait for."""
        with self._send_lock:
            self.link.mav.set_position_target_global_int_send(
                0, vehicle.system_id, 1,
                mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                IGNORE_ALL_BUT_POSITION,
                int(latitude_deg * DEGREES_TO_MAVLINK_INT),
                int(longitude_deg * DEGREES_TO_MAVLINK_INT),
                altitude_m,
                0, 0, 0, 0, 0, 0, 0, 0)


# --- the gateway itself ----------------------------------------------------

class Gateway:

    def __init__(self, bridge, ledger, command_port, telemetry_addresses, link):
        self.bridge = bridge
        self.ledger = ledger
        self.command_port = command_port
        # A unicast datagram goes to exactly one socket. Two processes binding
        # the same port do not share the stream, they steal from each other —
        # so every consumer gets its own address rather than a shared one.
        self.telemetry_addresses = list(telemetry_addresses)
        self.link = link
        self._running = True

    def serve_forever(self):
        threading.Thread(target=self._broadcast_telemetry, daemon=True).start()
        self._receive_commands()

    # -- telemetry ----------------------------------------------------------

    def _broadcast_telemetry(self):
        """Fire and forget, 5 Hz, to anyone bound to the telemetry port.

        Nobody acknowledges telemetry and nothing is retransmitted. A client
        that misses a sample waits 200 ms for the next one; a client that wants
        to know whether a command took effect reads the freshest sample rather
        than trusting an ack it may never get.
        """
        interval = 1.0 / TELEMETRY_HZ
        announced_stale = set()
        while self._running:
            for vehicle in self.bridge.vehicles_by_id.values():
                with vehicle.lock:
                    if not vehicle.heard_from:
                        continue
                    silent_for = time.time() - vehicle.last_message_at

                # Saying nothing is the honest answer. Continuing to broadcast
                # the last thing we heard would be indistinguishable, to every
                # client, from a healthy vehicle holding station — which is
                # exactly the lie the client's freshness check exists to catch,
                # and it cannot catch it if we keep the datagrams coming.
                if silent_for > TELEMETRY_STALE_AFTER_S:
                    if vehicle.vehicle_id not in announced_stale:
                        announced_stale.add(vehicle.vehicle_id)
                        log(f"  STALE: no MAVLink from {vehicle.vehicle_id} for "
                            f"{silent_for:.1f}s; telemetry for it is suspended "
                            f"until it speaks again")
                    continue
                if vehicle.vehicle_id in announced_stale:
                    announced_stale.discard(vehicle.vehicle_id)
                    log(f"  {vehicle.vehicle_id} is talking again")

                payload = vehicle.snapshot().SerializeToString()
                for address in self.telemetry_addresses:
                    self.link.send(payload, address, label="telemetry")
            time.sleep(interval)

    # -- commands -----------------------------------------------------------

    def _receive_commands(self):
        command_socket = self.link.socket
        destinations = ", ".join(f"{host}:{port}"
                                 for host, port in self.telemetry_addresses)
        log(f"listening for commands on udp/{self.command_port}, "
            f"telemetry to {destinations}")
        while self._running:
            try:
                datagram, sender = command_socket.recvfrom(65535)
            except OSError:
                break

            request = vehicle_pb2.ClientRequest()
            try:
                request.ParseFromString(datagram)
            except Exception as error:
                log(f"  undecodable datagram from {sender}: {error}")
                continue

            kind = request.WhichOneof('request')
            if kind == 'command':
                self._handle_command(request.command, sender)
            elif kind == 'status_query':
                self._handle_status_query(request.status_query, sender)
            else:
                log(f"  empty request from {sender}")

    def _handle_status_query(self, query, reply_address):
        """"What happened to operation X?" — the question a timeout provokes.

        Answering it is what turns a lost ack from a guess into a lookup.
        """
        recorded = self.ledger.lookup(query.operation_id)
        response = vehicle_pb2.GatewayResponse()
        response.status.operation_id = query.operation_id
        response.status.is_known = recorded is not None
        if recorded is not None:
            response.status.ack.CopyFrom(recorded)
        log(f"  status query {query.operation_id} -> "
            f"{'known' if recorded else 'never seen'}")
        # Deliberately not routed through drop_first_ack: this is the reply that
        # rescues a dropped ack, and breaking it would only test the test.
        self.link.send(response.SerializeToString(), reply_address,
                       label="status response")

    def _handle_command(self, command, reply_address):
        operation_id = command.operation_id or "<missing>"
        action = command.WhichOneof('action')
        log(f"command {operation_id} {command.vehicle_id} {action}")

        # 1. Duplicate? Answer from the ledger and touch nothing.
        recorded = self.ledger.lookup(operation_id)
        if recorded is not None:
            log(f"  duplicate of a command already answered "
                f"({vehicle_pb2.CommandStatus.Name(recorded.status)})")
            self._reply(self.ledger.replay(recorded), reply_address)
            return

        vehicle = self.bridge.vehicles_by_id.get(command.vehicle_id)
        if vehicle is None:
            known = ", ".join(sorted(self.bridge.vehicles_by_id))
            self._finish(command, vehicle_pb2.REJECTED_PERMANENT,
                         f"unknown vehicle_id; this gateway serves {known}",
                         0, reply_address)
            return

        # 2. Too old to mean anything? A command written against a world that
        #    has since moved on should not be executed just because it arrived.
        if command.issued_at_unix_ms:
            age_ms = now_unix_ms() - command.issued_at_unix_ms
            if age_ms > STALE_COMMAND_AGE_MS:
                self._finish(command, vehicle_pb2.REJECTED_PERMANENT,
                             f"command is {age_ms / 1000:.1f}s old; reissue it",
                             vehicle.state_version, reply_address)
                return

        # 3. Optimistic concurrency. The client says which version of the world
        #    it planned against; if that is not the world we are in, refuse and
        #    let it re-plan. This is what stops an approved-but-stale command.
        with vehicle.lock:
            current_version = vehicle.state_version
        if (command.expected_state_version
                and command.expected_state_version != current_version):
            self._finish(command, vehicle_pb2.STALE_STATE_VERSION,
                         f"planned against state_version "
                         f"{command.expected_state_version}, vehicle is at "
                         f"{current_version}",
                         current_version, reply_address)
            return

        if action is None:
            self._finish(command, vehicle_pb2.REJECTED_PERMANENT,
                         "no action set on the command", current_version,
                         reply_address)
            return

        # 4. Do the thing.
        status, reason = self._execute(action, command, vehicle)

        # A rejection is only useful if it says what to do instead, and the
        # aircraft usually just told us. Pass its words on rather than logging
        # them where only a human watching a terminal would see them.
        if status != vehicle_pb2.ACCEPTED:
            said = vehicle.recent_status_text()
            if said:
                reason = f"{reason}. The aircraft reported: {said}"

        # state_version moves only when the vehicle actually took the command,
        # so a client can count accepted writes and compare.
        version = (vehicle.bump_state_version() if status == vehicle_pb2.ACCEPTED
                   else current_version)
        self._finish(command, status, reason, version, reply_address)

    def _execute(self, action, command, vehicle):
        if action == 'arm':
            return self._arm(vehicle, arm=True)
        if action == 'disarm':
            return self._arm(vehicle, arm=False)
        if action == 'set_mode':
            mode_name = command.set_mode.mode_name
            # A misspelled mode never becomes spelled correctly by waiting, so
            # this is permanent, and the reason names the alternatives rather
            # than leaving the model to guess at them.
            if mode_name.upper() not in ACM_MODE_NUMBERS:
                return (vehicle_pb2.REJECTED_PERMANENT,
                        f"unknown flight mode '{mode_name}'; this vehicle "
                        f"accepts {', '.join(COMMON_MODES)}")
            ok, reason = self.bridge.send_mode_change(vehicle, mode_name)
            return (vehicle_pb2.ACCEPTED if ok
                    else vehicle_pb2.REJECTED_RETRYABLE), reason
        if action == 'takeoff':
            return self._takeoff(vehicle, command.takeoff.target_altitude_m)
        if action == 'goto_position':
            return self._goto(vehicle, command.goto_position)
        if action == 'return_to_launch':
            return self._simple_command(
                vehicle, mavutil.mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH)
        if action == 'land':
            return self._simple_command(vehicle, mavutil.mavlink.MAV_CMD_NAV_LAND)
        return vehicle_pb2.REJECTED_PERMANENT, f"unsupported action '{action}'"

    def _arm(self, vehicle, *, arm):
        what = "arm" if arm else "disarm"
        if arm:
            with vehicle.lock:
                mode = vehicle.flight_mode
            if mode in NON_ARMABLE_MODES:
                return (vehicle_pb2.REJECTED_RETRYABLE,
                        f"cannot arm while the vehicle is in {mode}, because "
                        f"that mode is already flying it; set_mode to GUIDED "
                        f"first, then arm")
        result = self.bridge.send_command_long(
            vehicle.system_id, mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            1.0 if arm else 0.0)
        status, reason = self._translate(result, what)
        if status != vehicle_pb2.ACCEPTED:
            return status, reason
        # The vehicle acknowledges the request a beat before HEARTBEAT reports
        # the new armed state, and heartbeats arrive at only 1 Hz. Returning
        # ACCEPTED on the ack alone lets a takeoff issued immediately afterwards
        # read a stale is_armed and be rejected. Confirm by readback, exactly as
        # set_mode does, so ACCEPTED means the same thing for every command.
        if self.bridge.await_armed_state(vehicle, arm):
            return vehicle_pb2.ACCEPTED, f"confirmed {what}ed"
        return (vehicle_pb2.REJECTED_RETRYABLE,
                f"vehicle acknowledged the {what} but had not reported it "
                f"within {ARM_CONFIRM_TIMEOUT_S}s; read telemetry before retrying")

    def _takeoff(self, vehicle, target_altitude_m):
        if target_altitude_m <= 0:
            return (vehicle_pb2.REJECTED_PERMANENT,
                    "target_altitude_m must be greater than 0")
        with vehicle.lock:
            if not vehicle.is_armed:
                return (vehicle_pb2.REJECTED_RETRYABLE,
                        "vehicle is not armed; arm first, then take off")
            if vehicle.flight_mode != 'GUIDED':
                return (vehicle_pb2.REJECTED_RETRYABLE,
                        f"takeoff needs GUIDED, vehicle is in "
                        f"{vehicle.flight_mode}")
            # A takeoff is a ground manoeuvre. The aircraft rejects this itself
            # but often with a bare FAILED, which tells a caller nothing it can
            # act on. Waiting does not help either, so this is permanent: the
            # plan is wrong, not early.
            if vehicle.altitude_m > AIRBORNE_ALTITUDE_M:
                return (vehicle_pb2.REJECTED_PERMANENT,
                        f"vehicle is already airborne at "
                        f"{vehicle.altitude_m:.1f}m; takeoff is only valid on "
                        f"the ground. To change altitude in flight, use a goto "
                        f"at the current position with the new altitude")
        result = self.bridge.send_command_long(
            vehicle.system_id, mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
            0, 0, 0, 0, 0, 0, target_altitude_m)      # param7 is the altitude
        return self._translate(result, f"takeoff to {target_altitude_m:.1f}m")

    def _goto(self, vehicle, target):
        with vehicle.lock:
            if not vehicle.is_armed:
                return (vehicle_pb2.REJECTED_RETRYABLE,
                        "vehicle is not armed")
            if vehicle.flight_mode != 'GUIDED':
                return (vehicle_pb2.REJECTED_RETRYABLE,
                        f"goto needs GUIDED, vehicle is in {vehicle.flight_mode}")
        self.bridge.send_position_target(
            vehicle, target.target_latitude_deg, target.target_longitude_deg,
            target.target_altitude_m)
        # No COMMAND_ACK exists for a setpoint, so ACCEPTED here means "handed
        # to the vehicle", and telemetry is how the client learns it arrived.
        return (vehicle_pb2.ACCEPTED,
                f"setpoint sent: {target.target_latitude_deg:.6f}, "
                f"{target.target_longitude_deg:.6f} at "
                f"{target.target_altitude_m:.1f}m; confirm by telemetry")

    def _simple_command(self, vehicle, command_id):
        result = self.bridge.send_command_long(vehicle.system_id, command_id)
        return self._translate(result, f"command {command_id}")

    def _translate(self, mav_result, what):
        if mav_result is None:
            # The gateway does not know. Saying so, and making the retry safe,
            # beats inventing either a success or a failure.
            return (vehicle_pb2.REJECTED_RETRYABLE,
                    f"no COMMAND_ACK for {what} within "
                    f"{MAVLINK_ACK_TIMEOUT_S}s; it may still have applied — "
                    f"read telemetry, and retry with the same operation_id")
        name = MAV_RESULT_NAMES.get(mav_result, str(mav_result))
        status = MAV_RESULT_TO_STATUS.get(mav_result, vehicle_pb2.REJECTED_PERMANENT)
        return status, f"vehicle answered {name} to {what}"

    def _finish(self, command, status, reason, state_version, reply_address):
        ack = vehicle_pb2.CommandAck(
            operation_id=command.operation_id,
            vehicle_id=command.vehicle_id,
            status=status,
            reason=reason,
            state_version=state_version,
            acked_at_unix_ms=now_unix_ms(),
        )
        # Recorded BEFORE it is sent. If the send is dropped the gateway must
        # still be able to answer "what happened to this operation?"
        self.ledger.remember(ack)
        log(f"  -> {vehicle_pb2.CommandStatus.Name(status)}: {reason}")
        self._reply(ack, reply_address, droppable=True)

    def _reply(self, ack, reply_address, droppable=False):
        response = vehicle_pb2.GatewayResponse()
        response.ack.CopyFrom(ack)
        self.link.send(response.SerializeToString(), reply_address,
                       operation_id=ack.operation_id if droppable else None,
                       label="ack")


# --- wiring ----------------------------------------------------------------

def parse_vehicle(text):
    """--vehicle copter_1=1 maps a name the model uses to a MAVLink system id."""
    name, _, system_id = text.partition('=')
    if not name or not system_id.isdigit():
        raise argparse.ArgumentTypeError(
            f"expected name=system_id, got '{text}'")
    return name, int(system_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mavlink', default='tcp:127.0.0.1:5760',
                        help='the router, not a copter (default: %(default)s)')
    parser.add_argument('--command-port', type=int, default=14550)
    parser.add_argument('--telemetry-to', action='append', metavar='HOST:PORT',
                        help='repeatable telemetry destination; default sends '
                             'to 14551 (the MCP tool server) and 14552 (the '
                             'harness executor) so both can listen at once')
    parser.add_argument('--vehicle', type=parse_vehicle, action='append',
                        metavar='NAME=SYSID',
                        help='repeatable; default: copter_1=1 copter_2=2')

    faults = parser.add_argument_group('link faults')
    faults.add_argument('--drop-first-ack', action='store_true',
                        help='swallow the first ack for each operation_id')
    faults.add_argument('--drop-rate', type=float, default=0.0)
    faults.add_argument('--dup', dest='duplicate_rate', type=float, default=0.0)
    faults.add_argument('--reorder', dest='reorder_rate', type=float, default=0.0)
    faults.add_argument('--delay-ms', type=int, default=0)
    faults.add_argument('--seed', type=int, default=None,
                        help='makes a lossy run reproducible')
    faults.add_argument('--battery-percent', type=float, default=None,
                        help='report this battery level regardless of what the '
                             'vehicle says; the simulator only drains by '
                             'flying, which makes a low-battery scenario slow '
                             'to set up and impossible to repeat')

    args = parser.parse_args()
    pairs = args.vehicle or [('copter_1', 1), ('copter_2', 2)]
    vehicles = {name: VehicleState(vehicle_id=name, system_id=system_id)
                for name, system_id in pairs}

    command_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    command_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    command_socket.bind(('0.0.0.0', args.command_port))

    link = LyingLink(command_socket,
                     drop_first_ack=args.drop_first_ack,
                     drop_rate=args.drop_rate,
                     duplicate_rate=args.duplicate_rate,
                     reorder_rate=args.reorder_rate,
                     delay_ms=args.delay_ms,
                     seed=args.seed)

    if args.battery_percent is not None:
        for state in vehicles.values():
            state.battery_override = args.battery_percent
        log(f"FAULT: reporting battery as {args.battery_percent}% for all vehicles")

    bridge = MavlinkBridge(args.mavlink, vehicles)
    destinations = []
    # 14551 the MCP tool server, 14552 the plan executor, 14553 the MCAP
    # recorder. One address per consumer: a unicast datagram goes to exactly
    # one socket, so listeners sharing a port steal from each other.
    for entry in (args.telemetry_to or ["127.0.0.1:14551", "127.0.0.1:14552",
                                        "127.0.0.1:14553"]):
        host, _, port = entry.rpartition(":")
        destinations.append((host or "127.0.0.1", int(port)))
    gateway = Gateway(bridge, CommandLedger(), args.command_port,
                      destinations, link)

    for name, state in vehicles.items():
        log(f"serving {name} as MAVLink system {state.system_id}")
    if args.drop_first_ack:
        log("FAULT: first ack per operation_id will be dropped")

    try:
        gateway.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
        gateway._running = False
        bridge.stop()


if __name__ == '__main__':
    main()
