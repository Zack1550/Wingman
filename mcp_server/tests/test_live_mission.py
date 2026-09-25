"""Layer two: the real gateway, the real router, a real aircraft.

Skipped unless something answers on udp/14550. These are slow by nature — an
EKF needs tens of seconds to settle and a climb takes as long as it takes — so
they are not what you run on every save.

    docker compose -f ardupilot_sitl_docker/stacks/n_copters/docker-compose-2.yml up -d
    python vehicle_gateway/gateway.py
    pytest -m live
"""
import time

import pytest

from vehicle_gateway.client import GatewayClient, GatewayUnavailable

pytestmark = pytest.mark.live

ARM_ATTEMPTS = 12          # pre-arm checks can refuse for a minute after boot
ARM_RETRY_WAIT_S = 8


def wait_for(client, vehicle_id, predicate, timeout_s, description):
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            last = client.telemetry(vehicle_id)
        except GatewayUnavailable:
            time.sleep(0.5)
            continue
        if predicate(last):
            return last
        time.sleep(0.5)
    pytest.fail(f"timed out waiting for {description}; last sample: {last}")


def settle(client, vehicle_id="copter_1", quiet_for_s=0.6, timeout_s=10):
    """Wait until telemetry stops changing, then return that state_version.

    The command path answers immediately; the 5 Hz telemetry broadcast that
    reflects it can be a couple of hundred milliseconds behind. Sampling a
    baseline without settling first reads the world as it was one command ago.
    """
    deadline = time.time() + timeout_s
    last, stable_since = None, time.time()
    while time.time() < deadline:
        current = client.telemetry(vehicle_id)["state_version"]
        if current != last:
            last, stable_since = current, time.time()
        elif time.time() - stable_since >= quiet_for_s:
            return current
        time.sleep(0.1)
    return last


def arm_with_retries(client, vehicle_id="copter_1"):
    """Retryable rejections are expected here, not a failure."""
    for _ in range(ARM_ATTEMPTS):
        outcome = client.send_command(GatewayClient.build(vehicle_id, "arm"))
        if outcome.status == "accepted":
            return outcome
        if not outcome.retryable:
            pytest.fail(f"arm permanently rejected: {outcome.reason}")
        time.sleep(ARM_RETRY_WAIT_S)
    pytest.fail("never armed within the retry budget")


@pytest.fixture
def grounded(live_client):
    """Leave the vehicle disarmed on the ground before and after each test."""
    yield live_client
    try:
        live_client.send_command(
            GatewayClient.build("copter_1", "return_to_launch"))
        wait_for(live_client, "copter_1", lambda t: not t["is_armed"],
                 180, "disarm after RTL")
    except Exception:
        pass


def test_both_vehicles_are_broadcasting(live_client):
    vehicles = live_client.known_vehicles()
    names = {v["vehicle_id"] for v in vehicles}
    assert {"copter_1", "copter_2"} <= names
    assert all(v["reachable"] for v in vehicles)


def test_telemetry_is_plausible(live_client):
    sample = live_client.telemetry("copter_1")
    assert sample["gps_fix_type"] >= 3, "no usable GPS fix"
    assert -90 <= sample["latitude_deg"] <= 90
    assert sample["age_s"] < 2.0


def test_takeoff_before_arming_is_rejected_retryably(live_client):
    """The rejection a model must be able to recover from."""
    outcome = live_client.send_command(
        GatewayClient.build("copter_2", "takeoff", target_altitude_m=10.0))
    assert outcome.status == "rejected"
    assert outcome.retryable is True
    assert "arm" in outcome.reason.lower()


def test_unknown_flight_mode_is_permanent(live_client):
    """Waiting never makes a misspelled mode valid."""
    outcome = live_client.send_command(
        GatewayClient.build("copter_1", "set_mode", mode_name="PLAID"))
    assert outcome.status == "rejected"
    assert outcome.retryable is False


def test_unknown_vehicle_is_permanent_and_lists_the_real_ones(live_client):
    outcome = live_client.send_command(GatewayClient.build("nonesuch", "arm"))
    assert outcome.status == "rejected"
    assert outcome.retryable is False
    assert "copter_1" in outcome.reason


@pytest.mark.slow
def test_a_whole_mission(grounded):
    """Mode, arm, climb, confirm by telemetry, come home."""
    client = grounded

    mode = client.send_command(
        GatewayClient.build("copter_1", "set_mode", mode_name="GUIDED"))
    assert mode.status == "accepted"

    armed = arm_with_retries(client)
    # The gateway confirms arming against its own MAVLink view before it
    # answers, but our telemetry is a separate 5 Hz stream, so give it a moment
    # to carry the news rather than asserting on a sample from before the ack.
    wait_for(client, "copter_1", lambda t: t["is_armed"], 10, "is_armed")

    takeoff = client.send_command(
        GatewayClient.build("copter_1", "takeoff", target_altitude_m=12.0))
    assert takeoff.status == "accepted"
    assert takeoff.state_version > armed.state_version

    # The ack said "starting", not "arrived". Telemetry decides.
    reached = wait_for(client, "copter_1",
                       lambda t: t["altitude_m"] >= 11.0, 90, "climb to 12 m")
    assert reached["flight_mode"] == "GUIDED"


@pytest.mark.slow
def test_state_version_advances_once_per_accepted_command(live_client):
    before = settle(live_client)
    outcome = live_client.send_command(
        GatewayClient.build("copter_1", "set_mode", mode_name="LOITER"))
    assert outcome.status == "accepted"
    assert outcome.state_version == before + 1, "the ack reports the new version"

    after = wait_for(live_client, "copter_1",
                     lambda t: t["state_version"] == before + 1,
                     5, "telemetry to carry the new state_version")
    assert after["state_version"] == before + 1


@pytest.mark.slow
def test_retry_against_the_real_gateway_applies_once(live_client):
    """The lost-ack property, proved against the real ledger."""
    before = settle(live_client)
    operation_id = live_client.new_operation_id()

    first = live_client.send_command(
        GatewayClient.build("copter_1", "set_mode", mode_name="GUIDED"),
        operation_id=operation_id)
    second = live_client.send_command(
        GatewayClient.build("copter_1", "set_mode", mode_name="GUIDED"),
        operation_id=operation_id)

    assert first.status == "accepted"
    assert second.gateway_status == "ALREADY_APPLIED"
    assert first.state_version == before + 1
    assert second.state_version == first.state_version

    after = settle(live_client)
    assert after == before + 1, "two datagrams must not move the world twice"
