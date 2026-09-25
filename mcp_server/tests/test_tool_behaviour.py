"""What the tools return, as the model would receive it.

The tool functions are called directly with the module's client swapped for one
pointed at a fake gateway. That keeps these fast while still exercising the real
argument handling, the real error shaping, and the real return dicts.
"""
import time

import pytest

from mcp_server import server
from mcp_server.tests.fake_gateway import FakeGateway
from vehicle_gateway.client import DEFAULT_TELEMETRY_MAX_AGE_S

import vehicle_pb2


@pytest.fixture
def tool_client(monkeypatch, fake_gateway, make_client):
    """Point the server module at the fake for the duration of one test."""
    client = make_client(fake_gateway)
    monkeypatch.setattr(server, "_client", client)
    return client


def test_get_telemetry_returns_units_not_prose(tool_client):
    result = server.get_telemetry("copter_1")
    assert "error" not in result
    for field in ("latitude_deg", "altitude_m", "flight_mode",
                  "state_version", "age_s"):
        assert field in result, f"missing {field}"
    assert isinstance(result["altitude_m"], float)


def test_list_vehicles_reports_reachability(tool_client):
    result = server.list_vehicles()
    names = {v["vehicle_id"] for v in result["vehicles"]}
    assert {"copter_1", "copter_2"} <= names
    assert all(v["reachable"] for v in result["vehicles"])


def test_unknown_vehicle_is_a_structured_error(tool_client):
    result = server.get_telemetry("ghost_9")
    assert result["error"] == "gateway_unavailable"
    assert "copter_1" in result["reason"]
    assert "advice" in result


def test_writes_share_one_shape(tool_client):
    result = server.arm("copter_1")
    assert set(result) >= {"op_id", "status", "reason", "retryable",
                           "state_version", "reconciled"}
    assert result["status"] in {"accepted", "rejected", "unknown"}


def test_write_returns_no_prose_field(tool_client):
    """Every value is data the model can branch on, not a sentence to parse."""
    result = server.takeoff("copter_1", 15.0)
    assert isinstance(result["status"], str)
    assert isinstance(result["retryable"], bool)
    assert isinstance(result["state_version"], int)


def test_op_id_is_assigned_per_call_and_differs(tool_client):
    first = server.arm("copter_1")
    second = server.arm("copter_1")
    assert first["op_id"] != second["op_id"], (
        "two separate calls are two commands and must not share an id")


def test_goto_passes_coordinates_through(tool_client, fake_gateway):
    result = server.goto("copter_1", 51.5018, -2.5518, 15.0)
    assert result["status"] == "accepted"
    assert len(fake_gateway.executed_operation_ids) == 1


def test_invalid_action_arguments_are_reported_structurally(tool_client):
    result = server._write("copter_1", "teleport")
    assert result["error"] == "invalid_arguments"


def test_get_command_status_round_trips(tool_client):
    written = server.arm("copter_1")
    looked_up = server.get_command_status(written["op_id"])
    assert looked_up["is_known"] is True
    assert looked_up["status"] == "accepted"
    assert "_ack" not in looked_up, "protobuf internals must not leak to the model"


def test_get_command_status_for_unknown_id(tool_client):
    result = server.get_command_status("op-never-happened")
    assert result["is_known"] is False


# --- the gateway going away ------------------------------------------------

def test_dead_gateway_reads_report_unavailable_not_stale(
        monkeypatch, telemetry_port, make_client):
    gateway = FakeGateway(telemetry_port=telemetry_port).start()
    client = make_client(gateway)
    monkeypatch.setattr(server, "_client", client)

    assert "error" not in server.get_telemetry("copter_1")
    gateway.stop()
    # Derived, not hardcoded: this limit moved once already when the transport's
    # real worst-case stall was measured, and a test that pins the old number
    # fails for the wrong reason.
    time.sleep(DEFAULT_TELEMETRY_MAX_AGE_S + 0.6)

    result = server.get_telemetry("copter_1")
    assert result["error"] == "gateway_unavailable"
    assert "altitude_m" not in result, "a dead gateway must not yield telemetry"


def test_dead_gateway_writes_are_unknown_not_failed(
        monkeypatch, telemetry_port, make_client):
    gateway = FakeGateway(telemetry_port=telemetry_port).start()
    client = make_client(gateway, ack_timeout_s=0.3)
    monkeypatch.setattr(server, "_client", client)
    gateway.stop()
    time.sleep(0.2)

    result = server.arm("copter_1")
    assert result["status"] == "unknown"
    assert result["retryable"] is True


def test_rejection_reason_survives_to_the_model(
        monkeypatch, telemetry_port, make_client):
    gateway = FakeGateway(telemetry_port=telemetry_port,
                          reject_with=vehicle_pb2.REJECTED_PERMANENT).start()
    try:
        client = make_client(gateway)
        monkeypatch.setattr(server, "_client", client)
        result = server.takeoff("copter_1", 10.0)
        assert result["status"] == "rejected"
        assert result["retryable"] is False
        assert result["reason"]
    finally:
        gateway.stop()
