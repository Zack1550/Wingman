"""The real gateway's dedup rule, with MAVLink replaced by a stub.

Everything else in these tests talks to FakeGateway, which copies the gateway's
behaviour, mistakes included: both used to replay a definite rejection forever,
so no test could see that a retry after "not armed yet" never ran again. This
file drives vehicle_gateway/gateway.py itself. Only the bridge (MAVLink) and
the link (UDP) are stubbed.
"""
import pytest

from vehicle_gateway import gateway as gw
from vehicle_gateway.client import GatewayClient

vehicle_pb2 = gw.vehicle_pb2


class StubBridge:
    """Answers COMMAND_LONG with whatever the test queues; counts the sends."""

    def __init__(self, vehicle):
        self.vehicles_by_id = {vehicle.vehicle_id: vehicle}
        self.answers = []            # MAV_RESULT per send, None = no ack
        self.sent = []

    def send_command_long(self, system_id, command_id, *params):
        self.sent.append(command_id)
        return self.answers.pop(0) if self.answers else 0


class StubLink:
    """Keeps every ack the gateway sends back, parsed."""

    def __init__(self):
        self.acks = []

    def send(self, payload, address, *, operation_id=None, label=""):
        response = vehicle_pb2.GatewayResponse()
        response.ParseFromString(payload)
        self.acks.append(response.ack)


@pytest.fixture
def world():
    vehicle = gw.VehicleState(vehicle_id="copter_1", system_id=1)
    vehicle.flight_mode = "GUIDED"
    vehicle.heard_from = True
    bridge = StubBridge(vehicle)
    link = StubLink()
    gateway = gw.Gateway(bridge, gw.CommandLedger(), 0, [], link)
    return gateway, vehicle, bridge, link


def takeoff(op_id):
    command = GatewayClient.build("copter_1", "takeoff", target_altitude_m=15.0)
    command.operation_id = op_id
    return command


def send(gateway, link, command):
    gateway._handle_command(command, ("127.0.0.1", 1))
    return link.acks[-1]


def status(ack):
    return vehicle_pb2.CommandStatus.Name(ack.status)


def test_a_retry_after_a_definite_rejection_is_evaluated_again(world):
    gateway, vehicle, bridge, link = world

    first = send(gateway, link, takeoff("op-1"))
    assert status(first) == "REJECTED_RETRYABLE"
    assert "not armed" in first.reason
    assert bridge.sent == [], "a precondition refusal sends nothing"

    vehicle.is_armed = True
    retry = send(gateway, link, takeoff("op-1"))

    assert status(retry) == "ACCEPTED", retry.reason
    assert len(bridge.sent) == 1


def test_a_status_query_still_reports_the_rejection(world):
    gateway, _, _, link = world
    send(gateway, link, takeoff("op-2"))

    recorded = gateway.ledger.lookup("op-2")
    assert status(recorded) == "REJECTED_RETRYABLE"


def test_an_accepted_command_is_never_applied_twice(world):
    gateway, vehicle, bridge, link = world
    vehicle.is_armed = True

    send(gateway, link, takeoff("op-3"))
    again = send(gateway, link, takeoff("op-3"))

    assert status(again) == "ALREADY_APPLIED"
    assert len(bridge.sent) == 1
    assert vehicle.state_version == 1


def test_an_unconfirmed_command_is_replayed_not_resent(world):
    """No COMMAND_ACK means it may have flown. Resending could fly it twice."""
    gateway, vehicle, bridge, link = world
    vehicle.is_armed = True
    bridge.answers = [None]

    first = send(gateway, link, takeoff("op-4"))
    assert status(first) == "REJECTED_RETRYABLE"
    assert "may still have applied" in first.reason

    again = send(gateway, link, takeoff("op-4"))
    assert status(again) == "REJECTED_RETRYABLE"
    assert len(bridge.sent) == 1, "the retry must be answered from the ledger"


def test_a_vehicle_that_said_no_is_asked_again(world):
    """TEMPORARILY_REJECTED is the vehicle refusing: nothing was applied."""
    gateway, vehicle, bridge, link = world
    vehicle.is_armed = True
    bridge.answers = [1, 0]          # TEMPORARILY_REJECTED, then ACCEPTED

    assert status(send(gateway, link, takeoff("op-5"))) == "REJECTED_RETRYABLE"
    assert status(send(gateway, link, takeoff("op-5"))) == "ACCEPTED"
    assert len(bridge.sent) == 2
