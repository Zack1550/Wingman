"""The gateway as a process: killed mid-mission, and cut off from its vehicle.

These run the real vehicle_gateway/gateway.py in a subprocess against
FakeVehicle, a MAVLink router on a real TCP port. Nothing is stubbed between the
harness's client and the MAVLink bytes, which is the point: a restart and a
dropped link are properties of processes and sockets, and a test that faked
either would prove only the fake.

Two policies live here, and both were chosen by the project owner:
    the ledger horizon     24 h, LEDGER_HORIZON_S in gateway.py
    the recovery bound     RECOVERY_BOUND_S below
"""
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness.executor import reconcile_outstanding
from harness.store import APPLIED, SUBMITTED, MissionStore
from mcp_server.tests.fake_gateway import free_udp_port
from mcp_server.tests.fake_vehicle import FakeVehicle, free_tcp_port
from vehicle_gateway.client import GatewayClient, GatewayUnavailable

import vehicle_pb2                                            # noqa: E402

REPO = Path(__file__).resolve().parents[2]
GATEWAY = REPO / "vehicle_gateway" / "gateway.py"
ARM = 400                      # MAV_CMD_COMPONENT_ARM_DISARM
TAKEOFF = 22                   # MAV_CMD_NAV_TAKEOFF

# Policy: once the router is back, the gateway must be serving fresh telemetry
# within this long. Covers the ~3.3 s stall this stack shows every ~34 s, a
# 1 s reconnect attempt, a 1 Hz heartbeat and the first sample, with margin.
RECOVERY_BOUND_S = 10.0


def wait_for(condition, timeout_s, interval_s=0.05):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(interval_s)
    return False


class GatewayProcess:
    """One run of gateway.py. start() again after kill() is a restart."""

    def __init__(self, tmp_path, vehicle_port, extra=()):
        self.vehicle_port = vehicle_port
        self.command_port = free_udp_port()
        self.telemetry_port = free_udp_port()
        self.ledger = tmp_path / "gateway_ledger.sqlite3"
        self.log_path = tmp_path / "gateway.log"
        self.extra = list(extra)
        self.process = None

    def start(self, extra=None):
        args = [sys.executable, str(GATEWAY),
                "--mavlink", f"tcp:127.0.0.1:{self.vehicle_port}",
                "--command-port", str(self.command_port),
                "--telemetry-to", f"127.0.0.1:{self.telemetry_port}",
                "--vehicle", "copter_1=1",
                "--ledger", str(self.ledger),
                *(self.extra if extra is None else extra)]
        log = self.log_path.open("a")
        self.process = subprocess.Popen(args, cwd=REPO, stdout=log,
                                        stderr=subprocess.STDOUT)
        return self

    def kill(self):
        """SIGKILL: no shutdown handler runs, exactly as in a crash."""
        self.process.kill()
        self.process.wait(timeout=5)

    def log(self):
        return self.log_path.read_text() if self.log_path.exists() else ""

    def wait_ready(self, starts=1, timeout_s=20):
        """Until the `starts`-th process has bound its port and is serving.

        Fresh telemetry is not enough after a restart: the client may still
        hold a sample the killed process sent a moment before it died.
        """
        return wait_for(
            lambda: self.log().count("listening for commands") >= starts,
            timeout_s)

    def recorded(self, operation_id):
        """What the ledger file holds for one op, read as a separate process would."""
        if not self.ledger.exists():
            return None
        with sqlite3.connect(self.ledger) as db:
            return db.execute("SELECT may_have_applied FROM acks "
                              "WHERE operation_id = ?",
                              (operation_id,)).fetchone()


@pytest.fixture
def vehicle():
    started = FakeVehicle(free_tcp_port()).start()
    yield started
    started.stop()


@pytest.fixture
def gateway(tmp_path, vehicle):
    made = GatewayProcess(tmp_path, vehicle.port)
    yield made
    if made.process is not None and made.process.poll() is None:
        made.kill()


def fresh_telemetry(client, max_age_s=1.0):
    try:
        return client.telemetry("copter_1", max_age_s=max_age_s)
    except GatewayUnavailable:
        return None


def listening_client(gateway, ack_timeout_s=2.0):
    client = GatewayClient(command_port=gateway.command_port,
                           telemetry_port=gateway.telemetry_port,
                           ack_timeout_s=ack_timeout_s).start()
    assert wait_for(lambda: fresh_telemetry(client), 20), gateway.log()
    return client


def send_without_waiting(gateway, command, operation_id):
    """Put a command on the wire the way a client does, and do not wait.

    The test needs the gateway to die between applying a command and anyone
    hearing about it; a blocking send would hold the test in the middle of
    exactly that window.
    """
    command.operation_id = operation_id
    command.issued_at_unix_ms = int(time.time() * 1000)
    request = vehicle_pb2.ClientRequest()
    request.command.CopyFrom(command)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(request.SerializeToString(),
                    ("127.0.0.1", gateway.command_port))


# --- a restart mid-mission --------------------------------------------------

def test_a_gateway_killed_mid_mission_does_not_dispatch_twice(
        tmp_path, vehicle, gateway):
    """The arm reaches the aircraft, the gateway dies, and nobody heard.

    The harness has the command in its store as submitted, the ack is lost
    (--drop-first-ack), and the gateway is SIGKILLed with the answer only on
    disk. After a restart, the harness does what a restarted harness must:
    reconcile before anything else. The aircraft must see exactly one arm, and
    the harness must learn it was applied rather than guess, retry, or resend.
    """
    gateway.start(extra=["--drop-first-ack"])
    client = listening_client(gateway)
    store = MissionStore(tmp_path / "mission.sqlite3")
    try:
        record = store.propose("run-restart", "copter_1", "arm",
                               {"vehicle_id": "copter_1"}, state_version=0)
        store.set_state(record.op_id, SUBMITTED)
        send_without_waiting(gateway, GatewayClient.build("copter_1", "arm"),
                             record.op_id)

        assert wait_for(lambda: vehicle.count(ARM) == 1, 15), gateway.log()
        assert wait_for(lambda: gateway.recorded(record.op_id), 15), \
            "the answer must be on disk before the gateway can die safely"
        gateway.kill()

        gateway.start(extra=[])
        assert gateway.wait_ready(starts=2), gateway.log()
        # Outlast the killed process's last sample, then wait for the new one.
        time.sleep(1.0)
        assert wait_for(lambda: fresh_telemetry(client, max_age_s=0.5), 20), \
            gateway.log()
        assert "resumes at state_version 1" in gateway.log(), \
            "a restart that reset versions to 0 would refuse every later command"

        # The restarted harness reconciles: a lookup, not a resend.
        resolved = reconcile_outstanding(store, client)
        assert resolved == [(record.op_id, APPLIED)]
        assert store.command(record.op_id).state == APPLIED

        # A retry of the same op_id is answered from the ledger, not flown.
        replay = client.send_command(GatewayClient.build("copter_1", "arm"),
                                     operation_id=record.op_id)
        assert replay.gateway_status == "ALREADY_APPLIED", replay.reason
        assert vehicle.count(ARM) == 1, "the arm was dispatched twice"

        # And the mission carries on from where the dead process left it.
        climb = client.send_command(GatewayClient.build(
            "copter_1", "takeoff", target_altitude_m=10.0))
        assert climb.status == "accepted", climb.reason
        assert climb.state_version == 2
        assert vehicle.count(TAKEOFF) == 1
    finally:
        store.close()
        client.close()


# --- a dropped MAVLink link -------------------------------------------------

def test_a_dropped_mavlink_link_recovers_within_the_bound(vehicle, gateway):
    """The router dies and comes back; the gateway reconnects on its own.

    While the link is down a command is refused before anything is sent, so
    the refusal is definite and nothing is left "may have applied". Once the
    router is back, fresh telemetry, from after the restart, must reach the
    client within RECOVERY_BOUND_S, and commands must work again.
    """
    gateway.start()
    client = listening_client(gateway)
    try:
        assert fresh_telemetry(client)["latitude_deg"] == pytest.approx(
            vehicle.latitude_for(1), abs=1e-6)

        vehicle.stop()
        assert wait_for(lambda: "LINK DOWN" in gateway.log(), 10), gateway.log()

        refused = client.send_command(GatewayClient.build("copter_1", "arm"))
        assert refused.status == "rejected" and refused.retryable, refused.reason
        assert "link" in refused.reason and "nothing was sent" in refused.reason
        assert vehicle.count(ARM) == 0

        vehicle.start()
        restored_at = time.time()

        def telemetry_from_after_the_restart():
            sample = fresh_telemetry(client)
            return sample is not None and sample["latitude_deg"] == \
                pytest.approx(vehicle.latitude_for(2), abs=1e-6)

        assert wait_for(telemetry_from_after_the_restart, RECOVERY_BOUND_S), (
            f"no fresh telemetry within {RECOVERY_BOUND_S}s of the router "
            f"coming back\n{gateway.log()}")
        recovered_in = time.time() - restored_at
        assert "LINK UP" in gateway.log()

        armed = client.send_command(GatewayClient.build("copter_1", "arm"))
        assert armed.status == "accepted", armed.reason
        assert vehicle.count(ARM) == 1
        print(f"recovered in {recovered_in:.1f}s (bound {RECOVERY_BOUND_S}s)")
    finally:
        client.close()
