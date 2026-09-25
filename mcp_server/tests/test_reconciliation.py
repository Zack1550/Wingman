"""What the client does when the link misbehaves.

Every test here runs against the in-process fake, so a dropped ack is a flag
rather than a race. The assertions are mostly about what did NOT happen: the
vehicle did not move twice, the outcome was not invented.
"""
import time

import pytest

from vehicle_gateway.client import GatewayClient, GatewayUnavailable
from mcp_server.tests.fake_gateway import FakeGateway, free_udp_port

import vehicle_pb2


def takeoff_command(vehicle_id="copter_1", altitude_m=10.0):
    return GatewayClient.build(vehicle_id, "takeoff",
                               target_altitude_m=altitude_m)


# --- the happy path --------------------------------------------------------

def test_accepted_command_reports_accepted(client, fake_gateway):
    outcome = client.send_command(takeoff_command())
    assert outcome.status == "accepted"
    assert outcome.reconciled is False
    assert outcome.state_version == 1
    assert fake_gateway.executed_operation_ids == [outcome.operation_id]


def test_client_assigns_an_operation_id(client):
    outcome = client.send_command(takeoff_command())
    assert outcome.operation_id.startswith("op-")


# --- a lost reply ----------------------------------------------------------

def test_dropped_ack_is_reconciled_not_guessed(telemetry_port, make_client):
    gateway = FakeGateway(telemetry_port=telemetry_port,
                          drop_first_ack=True).start()
    try:
        client = make_client(gateway, ack_timeout_s=0.5)
        outcome = client.send_command(takeoff_command())

        assert outcome.status == "accepted"
        assert outcome.reconciled is True, "should have asked, not assumed"
        assert gateway.status_queries == [outcome.operation_id]
        assert len(gateway.executed_operation_ids) == 1
    finally:
        gateway.stop()


def test_silent_gateway_yields_unknown_never_a_guess(telemetry_port, make_client):
    """The outcome the whole design exists for: we do not know, and we say so."""
    gateway = FakeGateway(telemetry_port=telemetry_port, silent=True).start()
    try:
        client = make_client(command_port=gateway.command_port,
                             ack_timeout_s=0.3)
        outcome = client.send_command(takeoff_command())

        assert outcome.status == "unknown"
        assert outcome.status != "rejected", "silence is not failure"
        assert outcome.retryable is True
        assert "may or may not" in outcome.reason
    finally:
        gateway.stop()


def test_unknown_outcome_still_carries_its_operation_id(telemetry_port, make_client):
    """Without the id the caller cannot reconcile later, so it must survive."""
    gateway = FakeGateway(telemetry_port=telemetry_port, silent=True).start()
    try:
        client = make_client(command_port=gateway.command_port,
                             ack_timeout_s=0.3)
        outcome = client.send_command(takeoff_command())
        assert outcome.operation_id
        assert outcome.as_dict()["op_id"] == outcome.operation_id
    finally:
        gateway.stop()


# --- idempotency -----------------------------------------------------------

def test_retrying_one_operation_id_executes_once(client, fake_gateway):
    """Three datagrams, one logical command."""
    command = takeoff_command()
    operation_id = client.new_operation_id()

    first = client.send_command(command, operation_id=operation_id)
    second = client.send_command(takeoff_command(), operation_id=operation_id)
    third = client.send_command(takeoff_command(), operation_id=operation_id)

    assert [first.status, second.status, third.status] == ["accepted"] * 3
    assert len(fake_gateway.received_operation_ids) == 3
    assert len(fake_gateway.executed_operation_ids) == 1
    assert fake_gateway.state_version("copter_1") == 1
    assert second.gateway_status == "ALREADY_APPLIED"


def test_distinct_operation_ids_execute_separately(client, fake_gateway):
    """The other half of the rule: a different command must not be swallowed."""
    client.send_command(takeoff_command(altitude_m=10.0))
    client.send_command(takeoff_command(altitude_m=20.0))
    assert len(fake_gateway.executed_operation_ids) == 2
    assert fake_gateway.state_version("copter_1") == 2


# --- rejections ------------------------------------------------------------

@pytest.mark.parametrize("gateway_status,expected,retryable", [
    (vehicle_pb2.REJECTED_RETRYABLE, "rejected", True),
    (vehicle_pb2.REJECTED_PERMANENT, "rejected", False),
    (vehicle_pb2.STALE_STATE_VERSION, "rejected", True),
])
def test_rejections_map_to_actionable_outcomes(telemetry_port, make_client,
                                               gateway_status, expected,
                                               retryable):
    gateway = FakeGateway(telemetry_port=telemetry_port,
                          reject_with=gateway_status).start()
    try:
        client = make_client(gateway)
        outcome = client.send_command(takeoff_command())
        assert outcome.status == expected
        assert outcome.retryable is retryable
    finally:
        gateway.stop()


def test_rejected_command_does_not_advance_state_version(telemetry_port,
                                                         make_client):
    gateway = FakeGateway(telemetry_port=telemetry_port,
                          reject_with=vehicle_pb2.REJECTED_PERMANENT).start()
    try:
        client = make_client(gateway)
        client.send_command(takeoff_command())
        assert gateway.state_version("copter_1") == 0
    finally:
        gateway.stop()


# --- status queries --------------------------------------------------------

def test_status_query_for_an_unseen_operation_says_so(client):
    result = client.command_status("op-never-sent")
    assert result["is_known"] is False


def test_status_query_recovers_a_recorded_outcome(client):
    outcome = client.send_command(takeoff_command())
    recovered = client.command_status(outcome.operation_id)
    assert recovered["is_known"] is True
    assert recovered["status"] == "accepted"
    assert recovered["state_version"] == outcome.state_version


# --- telemetry freshness ---------------------------------------------------

def test_telemetry_reads_back(client):
    sample = client.telemetry("copter_1")
    assert sample["vehicle_id"] == "copter_1"
    assert sample["gps_fix_type"] == 6
    assert sample["age_s"] < 1.0


def test_stale_telemetry_raises_rather_than_returning_the_last_sample(
        client, fake_gateway):
    assert client.telemetry("copter_1")["vehicle_id"] == "copter_1"
    fake_gateway.telemetry_enabled = False
    time.sleep(0.4)

    with pytest.raises(GatewayUnavailable) as caught:
        client.telemetry("copter_1", max_age_s=0.2)
    assert "unreachable" in str(caught.value)


def test_unknown_vehicle_names_the_ones_that_exist(client):
    with pytest.raises(GatewayUnavailable) as caught:
        client.telemetry("ghost_9")
    message = str(caught.value)
    assert "copter_1" in message, "a rejection should say what IS valid"


def test_known_vehicles_marks_silence_as_unreachable(client, fake_gateway):
    assert all(v["reachable"] for v in client.known_vehicles())
    fake_gateway.telemetry_enabled = False
    time.sleep(0.4)
    assert not any(v["reachable"] for v in
                   client.known_vehicles(max_age_s=0.2))


# --- a gateway that is not there at all ------------------------------------

def test_no_gateway_at_all_is_unknown_not_rejected(telemetry_port):
    dead_port = free_udp_port()
    client = GatewayClient(command_port=dead_port,
                           telemetry_port=telemetry_port,
                           ack_timeout_s=0.3).start()
    try:
        outcome = client.send_command(takeoff_command())
        assert outcome.status == "unknown"
    finally:
        client.close()


def test_no_telemetry_ever_is_reported_as_such(telemetry_port):
    dead_port = free_udp_port()
    client = GatewayClient(command_port=dead_port,
                           telemetry_port=telemetry_port,
                           ack_timeout_s=0.3).start()
    try:
        with pytest.raises(GatewayUnavailable) as caught:
            client.telemetry("copter_1")
        assert "has ever arrived" in str(caught.value)
    finally:
        client.close()
