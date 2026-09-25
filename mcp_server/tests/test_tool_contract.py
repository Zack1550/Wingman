"""What the model is allowed to see.

These tests never touch a socket. They assert on the tool surface itself,
because the schema is the part of the harness a model can actually exploit: a
field it should not control, a tool it should not reach, a description too thin
to act on.
"""
import asyncio

import pytest

from mcp_server import server

EXPECTED_TOOLS = {
    "list_vehicles", "get_telemetry", "get_command_status",
    "wait_for_altitude", "wait_for_disarm", "wait_for_position",
    "set_mode", "arm", "disarm", "takeoff", "goto",
    "return_to_launch", "land",
}

WRITE_TOOLS = {"set_mode", "arm", "disarm", "takeoff", "goto",
               "return_to_launch", "land"}

# Fields the runtime owns. A model that could set these could defeat
# deduplication by varying an id, or claim an approval it was never granted.
HOST_CONTROLLED_FIELDS = {"op_id", "operation_id", "approval_token",
                          "approval", "state_version", "expected_state_version",
                          "issued_at_unix_ms"}


@pytest.fixture(scope="module")
def tools():
    return {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}


def test_exposes_exactly_the_expected_tools(tools):
    assert set(tools) == EXPECTED_TOOLS


@pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
def test_write_tools_do_not_accept_host_controlled_fields(tools, name):
    """The one invariant that keeps idempotency out of the model's hands."""
    properties = set(tools[name].input_schema.get("properties", {}))
    leaked = properties & HOST_CONTROLLED_FIELDS
    assert not leaked, f"{name} exposes host-controlled field(s): {leaked}"


def test_get_command_status_does_take_an_op_id(tools):
    """The read side is the exception, and deliberately so.

    The model only ever quotes back an id a write tool already handed it, which
    is what lets it reconcile an unknown outcome.
    """
    assert "op_id" in tools["get_command_status"].input_schema["properties"]


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_every_tool_has_a_description_worth_reading(tools, name):
    description = tools[name].description or ""
    assert len(description) > 80, f"{name} description is too thin to be a prompt"


@pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
def test_every_write_tool_names_its_vehicle(tools, name):
    assert "vehicle_id" in tools[name].input_schema.get("properties", {})
    assert "vehicle_id" in tools[name].input_schema.get("required", [])


def test_takeoff_declares_units_and_altitude_reference(tools):
    """Units in the description, because the model cannot see the field type."""
    description = tools["takeoff"].description.lower()
    assert "metre" in description or "meter" in description
    assert "launch" in description          # above launch, not above sea level


def test_goto_warns_that_acceptance_is_not_arrival(tools):
    description = tools["goto"].description.lower()
    assert "telemetry" in description


def test_server_instructions_state_the_ground_truth_rule():
    instructions = (server.mcp.instructions or "").lower()
    assert "telemetry" in instructions
    assert "unknown" in instructions
