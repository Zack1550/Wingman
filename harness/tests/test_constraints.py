"""Limits enforced in code, on every path that can command an aircraft.

The interesting tests here are not "the fence works". They are the ones that
prove the fence cannot be talked, approved, or routed around.
"""
import time

import pytest

from harness.approval import AutoApprovalGate
from harness.constraints import (Constraints, DEFAULT_FENCE, Refusal,
                                 point_in_polygon)
from harness.executor import Executor
from harness.planner import Plan, PlanStep
from harness.store import REJECTED
from harness.tests.conftest import RUN

HOME = (51.501592, -2.551791)
FAR_AWAY = (51.60000, -2.40000)


@pytest.fixture
def limits():
    return Constraints(fence=list(DEFAULT_FENCE), max_altitude_m=60.0)


def plan_of(*steps):
    return Plan(summary="test plan", steps=list(steps))


def goto_step(latitude, longitude, altitude=30.0):
    return PlanStep(tool="goto",
                    args={"vehicle_id": "copter_1", "latitude_deg": latitude,
                          "longitude_deg": longitude, "altitude_m": altitude},
                    rationale="fly there", risk="high")


def takeoff_step(altitude):
    return PlanStep(tool="takeoff",
                    args={"vehicle_id": "copter_1",
                          "target_altitude_m": altitude},
                    rationale="climb", risk="high")


# --- the geometry -----------------------------------------------------------

def test_the_launch_site_is_inside_the_fence():
    assert point_in_polygon(HOME[0], HOME[1], DEFAULT_FENCE)


def test_a_point_far_away_is_outside():
    assert not point_in_polygon(FAR_AWAY[0], FAR_AWAY[1], DEFAULT_FENCE)


@pytest.mark.parametrize("latitude,longitude", [
    (51.49980, -2.55180),      # on the south edge
    (51.50340, -2.55180),      # on the north edge
    (51.50160, -2.55380),      # on the west edge
    (51.49980, -2.55380),      # exactly on a corner
])
def test_a_point_on_the_boundary_counts_as_inside(latitude, longitude):
    """Decided on purpose: a waypoint on the line should not flip on rounding."""
    assert point_in_polygon(latitude, longitude, DEFAULT_FENCE)


def test_just_outside_is_outside():
    assert not point_in_polygon(51.49979, -2.55180, DEFAULT_FENCE)


# --- the rules --------------------------------------------------------------

def test_a_goto_inside_the_fence_is_permitted(limits):
    assert limits.check("goto", {"latitude_deg": HOME[0],
                                 "longitude_deg": HOME[1],
                                 "altitude_m": 30.0}) is None


def test_a_goto_outside_the_fence_is_refused(limits):
    refusal = limits.check("goto", {"latitude_deg": FAR_AWAY[0],
                                    "longitude_deg": FAR_AWAY[1],
                                    "altitude_m": 30.0})
    assert refusal.rule == "geofence"
    assert "outside the operating area" in refusal.reason


def test_a_takeoff_above_the_ceiling_is_refused(limits):
    refusal = limits.check("takeoff", {"target_altitude_m": 120.0})
    assert refusal.rule == "altitude_cap"
    assert refusal.limit == 60.0
    assert refusal.offered == 120.0


def test_a_goto_inside_the_fence_but_too_high_is_still_refused(limits):
    """Two limits, and satisfying one is not satisfying the other."""
    refusal = limits.check("goto", {"latitude_deg": HOME[0],
                                    "longitude_deg": HOME[1],
                                    "altitude_m": 200.0})
    assert refusal.rule == "altitude_cap"


def test_the_fence_is_checked_before_the_ceiling(limits):
    """A command that breaks both should name the more fundamental problem."""
    refusal = limits.check("goto", {"latitude_deg": FAR_AWAY[0],
                                    "longitude_deg": FAR_AWAY[1],
                                    "altitude_m": 200.0})
    assert refusal.rule == "geofence"


def test_arming_outside_the_fence_is_refused(limits):
    """Where it already is matters, even for a command with no coordinates."""
    refusal = limits.check("arm", {"vehicle_id": "copter_1"},
                           telemetry={"latitude_deg": FAR_AWAY[0],
                                      "longitude_deg": FAR_AWAY[1]})
    assert refusal.rule == "geofence"


def test_arming_inside_the_fence_is_fine(limits):
    assert limits.check("arm", {"vehicle_id": "copter_1"},
                        telemetry={"latitude_deg": HOME[0],
                                   "longitude_deg": HOME[1]}) is None


def test_a_refusal_says_what_to_do_about_it(limits):
    payload = limits.check("takeoff", {"target_altitude_m": 500.0}).as_dict()
    assert payload["error"] == "constraint_violation"
    assert payload["rule"] == "altitude_cap"
    assert payload["limit"] == 60.0
    assert payload["you_asked_for"] == 500.0
    assert "cannot be approved" in payload["advice"]


def test_reads_are_never_constrained(limits):
    for tool in ("get_telemetry", "list_vehicles", "wait_for_altitude"):
        assert limits.check(tool, {"vehicle_id": "copter_1"}) is None


# --- the part that matters: it cannot be routed around ----------------------

def test_an_approved_command_outside_the_fence_still_does_not_fly(
        store, client, gateway, limits):                     
    """The operator said yes. The fence does not care, and must not."""
    executor = Executor(store, AutoApprovalGate(grant=True), client=client,
                        constraints=limits)
    result = executor.run(plan_of(goto_step(*FAR_AWAY)), RUN)

    assert result.stop_reason == "blocked"
    assert result.outcomes[0].status == "blocked"
    assert "geofence" in result.outcomes[0].reason
    assert gateway.executed_operation_ids == [], "nothing may reach the vehicle"


def test_the_operator_is_never_even_asked(store, client, gateway, limits):
    """A prompt for something the system will refuse teaches people to click yes."""
    gate = AutoApprovalGate(grant=True)
    Executor(store, gate, client=client, constraints=limits).run(
        plan_of(takeoff_step(500.0)), RUN)

    assert gate.seen == [], "no approval should have been requested"


def test_a_blocked_command_is_recorded_with_its_rule(store, client, gateway,
                                                     limits):
    Executor(store, AutoApprovalGate(grant=True), client=client,
             constraints=limits).run(plan_of(takeoff_step(500.0)), RUN)

    record = store.commands_for_run(RUN)[0]
    assert record.state == REJECTED
    assert record.reason.startswith("altitude_cap:")


def test_a_legal_command_still_flies(store, client, gateway, limits):
    """The fence must refuse the illegal case without breaking the legal one."""
    result = Executor(store, AutoApprovalGate(grant=True), client=client,
                      constraints=limits).run(
        plan_of(goto_step(HOME[0], HOME[1], altitude=30.0)), RUN)

    assert result.completed
    assert len(gateway.executed_operation_ids) == 1


def test_one_illegal_step_stops_the_rest_of_the_plan(store, client, gateway,
                                                     limits):
    result = Executor(store, AutoApprovalGate(grant=True), client=client,
                      constraints=limits).run(
        plan_of(goto_step(*FAR_AWAY), goto_step(HOME[0], HOME[1])), RUN)

    assert result.stop_reason == "blocked"
    assert len(result.outcomes) == 1
    assert gateway.executed_operation_ids == []


# --- the fleet rule: the first one that needs more than one aircraft --------

BRAVO_AIRBORNE = {"vehicle_id": "copter_2", "latitude_deg": 51.501592,
                  "longitude_deg": -2.551791, "altitude_m": 20.0}


def fleet_with(*vehicles):
    return {v["vehicle_id"]: v for v in vehicles}


def test_a_goto_far_from_the_other_aircraft_is_permitted(limits):
    assert limits.check(
        "goto", {"vehicle_id": "copter_1", "latitude_deg": 51.502592,
                 "longitude_deg": -2.551791, "altitude_m": 20.0},
        fleet=fleet_with(BRAVO_AIRBORNE)) is None


def test_flying_into_another_aircraft_is_refused(limits):
    """Same point, same height: the case the rule exists for."""
    refusal = limits.check(
        "goto", {"vehicle_id": "copter_1", "latitude_deg": 51.501592,
                 "longitude_deg": -2.551791, "altitude_m": 20.0},
        fleet=fleet_with(BRAVO_AIRBORNE))
    assert refusal.rule == "separation"
    assert "copter_2" in refusal.reason


def test_stacking_vertically_above_another_aircraft_is_allowed(limits):
    """A cylinder, not a sphere. Overflying at a different height is normal."""
    assert limits.check(
        "goto", {"vehicle_id": "copter_1", "latitude_deg": 51.501592,
                 "longitude_deg": -2.551791, "altitude_m": 40.0},
        fleet=fleet_with(BRAVO_AIRBORNE)) is None


def test_close_horizontally_and_vertically_is_refused(limits):
    refusal = limits.check(
        "goto", {"vehicle_id": "copter_1", "latitude_deg": 51.501592,
                 "longitude_deg": -2.551791, "altitude_m": 22.0},
        fleet=fleet_with(BRAVO_AIRBORNE))
    assert refusal.rule == "separation"
    assert refusal.offered[1] == 2.0        # only 2 m of height between them


def test_an_aircraft_is_not_its_own_traffic(limits):
    itself = dict(BRAVO_AIRBORNE, vehicle_id="copter_1")
    assert limits.check(
        "goto", {"vehicle_id": "copter_1", "latitude_deg": 51.501592,
                 "longitude_deg": -2.551791, "altitude_m": 20.0},
        fleet=fleet_with(itself)) is None


def test_the_parked_aircraft_do_not_violate_the_rule(limits):
    """copter_1 and copter_2 sit 13.1 m apart in this stack, by design.

    A separation limit above that would refuse every command before either
    aircraft moved — including arming — and would look like a broken rule
    rather than a badly chosen number.
    """
    parked = fleet_with(
        {"vehicle_id": "copter_1", "latitude_deg": 51.501592,
         "longitude_deg": -2.551791, "altitude_m": 0.0},
        {"vehicle_id": "copter_2", "latitude_deg": 51.501692,
         "longitude_deg": -2.551691, "altitude_m": 0.0})
    assert limits.check(
        "takeoff", {"vehicle_id": "copter_1", "target_altitude_m": 15.0},
        telemetry=parked["copter_1"], fleet=parked) is None


def test_separation_is_enforced_on_the_approved_plan_path(store, client,
                                                          gateway, limits):
    """The operator approves; the fleet rule still refuses."""
    # The fake reports every vehicle at HOME on the ground, so a goto to that
    # point at ground level is squarely inside copter_2's cylinder.
    fleet_point = goto_step(HOME[0], HOME[1], altitude=0.5)
    executor = Executor(store, AutoApprovalGate(grant=True), client=client,
                        constraints=limits)
    # The fake gateway reports both vehicles at the same spot, so any goto to
    # that point is inside copter_2's cylinder.
    result = executor.run(plan_of(fleet_point), RUN)

    assert result.stop_reason == "blocked"
    assert "separation" in result.outcomes[0].reason
    assert gateway.executed_operation_ids == []
