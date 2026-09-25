#!/usr/bin/env python3
"""MCP tool server for the vehicle gateway.

This is the surface the model sees, so the docstrings below are prompts, not
documentation. Each one carries units, ranges, and what a rejection means,
because a tool that returns "failed" teaches the model nothing and a tool that
returns "not armed; call arm first" teaches it exactly one thing.

Three rules hold throughout:

  Structured, never prose.  Every tool returns a dict. Nothing returns an
  English sentence for the model to parse back into a decision.

  Runtime fields are not model-facing.  op_id is assigned here, by the host, and
  never appears in a tool's arguments. A model that could choose its own
  operation ids could defeat deduplication by varying them, or replay someone
  else's by guessing them.

  Unreachable is not the same as unchanged.  A dead gateway returns a structured
  error saying so. It never returns the last telemetry we happened to hear.

Run:
    python mcp_server/server.py                      # stdio, for a host
    npx @modelcontextprotocol/inspector python mcp_server/server.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.constraints import DEFAULT_CONSTRAINTS            # noqa: E402
from mcp.server import MCPServer                               # noqa: E402
from vehicle_gateway.client import (                           # noqa: E402
    GatewayClient,
    GatewayUnavailable
)

mcp = MCPServer(
    "vehicle-gateway",
    instructions=(
        "Commands simulated multirotor aircraft through a UDP gateway. "
        "Telemetry is the only ground truth: an accepted command means the "
        "vehicle took it, not that the manoeuvre finished. After any write, "
        "read get_telemetry to confirm the effect before reporting success. "
        "A command whose status is 'unknown' may or may not have applied — "
        "never assume either way; call get_command_status or read telemetry."
    ),
)

_client = None


def client():
    global _client
    if _client is None:
        _client = GatewayClient().start()
    return _client


def _unavailable(error):
    """The one shape every tool uses to say 'I could not find out'."""
    return {
        "error": "gateway_unavailable",
        "reason": str(error),
        "advice": "Do not assume vehicle state. Stop and report this.",
    }


# --- reads -----------------------------------------------------------------

@mcp.tool()
def list_vehicles() -> dict:
    """List the aircraft currently broadcasting telemetry.

    A vehicle appears here only if the gateway has heard from it, so this is a
    list of vehicles you can actually command, not a configured roster. Check
    'reachable': a vehicle with reachable=false has stopped broadcasting and
    must not be commanded.

    Returns {"vehicles": [{vehicle_id, reachable, last_seen_s_ago,
    flight_mode, is_armed, state_version}]}.
    """
    vehicles = client().known_vehicles()
    if not vehicles:
        return {
            "vehicles": [],
            "warning": ("no telemetry has arrived from any vehicle. The "
                        "gateway may not be running."),
        }
    return {"vehicles": vehicles}


@mcp.tool()
def get_telemetry(vehicle_id: str) -> dict:
    """Read the freshest telemetry for one aircraft.

    This is the only ground truth about what a vehicle is doing. Use it to
    confirm that a command took effect rather than trusting the command's own
    acknowledgement.

    Fields: latitude_deg / longitude_deg in plain degrees (WGS-84);
    altitude_m above the launch point, not sea level; ground_speed_mps;
    heading_deg 0-360 clockwise from north; flight_mode such as GUIDED,
    LOITER, RTL; is_armed; gps_fix_type where 3 or more is usable;
    battery_percent 0-100; state_version, which increments on every accepted
    command and is how you detect that the world changed; age_s, how old this
    sample is.

    Returns a structured error if telemetry is missing or older than 2 s. That
    means the vehicle is unreachable — it does not mean nothing has changed.
    """
    try:
        return client().telemetry(vehicle_id)
    except GatewayUnavailable as error:
        return _unavailable(error)


@mcp.tool()
def get_command_status(op_id: str) -> dict:
    """Ask the gateway what became of a command, by its op_id.

    Call this when a write returned status "unknown". The gateway records every
    command's outcome before replying, so it can answer even when its reply to
    you was lost in transit.

    is_known=false means the gateway never saw that operation, so the command
    did not arrive and it is safe to issue it again.

    Returns {op_id, is_known, status, reason, retryable, state_version}.
    """
    try:
        result = client().command_status(op_id)
    except GatewayUnavailable as error:
        return _unavailable(error)
    result.pop("_ack", None)
    return result


# --- waiting ---------------------------------------------------------------
#
# These are reads, not commands: no op_id, no ledger entry, nothing moves. They
# exist because "wait until it gets there" is the one thing a model reliably
# says it will do and then does not. Putting the polling loop inside a tool call
# makes the waiting a property of the system instead of a hope.
#
# The waits are deliberately SHORT. A long block freezes the model until it ends,
# hides a stalled climb, and gives it nothing to reason about; a short one hands
# back progress (9 m, then 12 m, then 14.3 m) and lets it decide whether to keep
# waiting or do something else.

MAX_WAIT_S = 20.0          # stays well under the harness dispatch timeout
POLL_INTERVAL_S = 0.4      # telemetry arrives at 5 Hz
STALLED_CHANGE_M = 0.3
ARRIVAL_TOLERANCE_M = 15.0         # not a tool argument, for the same reason


def _metres_between(sample, latitude_deg, longitude_deg):
    north = (sample["latitude_deg"] - latitude_deg) * 111_000
    east = (sample["longitude_deg"] - longitude_deg) * 111_000 * 0.62
    return (north ** 2 + east ** 2) ** 0.5
# The tolerance is NOT a tool argument. Offered the choice, a model picks a band
# wide enough to pass: asked for 45 m it chose 40-50 and reported success at
# 40.6 m while still climbing. The tool owns the criterion; the model supplies
# only the target it was given.
ALTITUDE_TOLERANCE_M = 1.0


def _poll_until(vehicle_id, is_done, timeout_s):
    """Shared machinery. The tools above it stay narrow on purpose."""
    capped = max(0.5, min(float(timeout_s), MAX_WAIT_S))
    started = time.time()
    first = client().telemetry(vehicle_id)
    sample = first
    while True:
        sample = client().telemetry(vehicle_id)
        if is_done(sample):
            return True, first, sample, time.time() - started, capped
        if time.time() - started >= capped:
            return False, first, sample, time.time() - started, capped
        time.sleep(POLL_INTERVAL_S)


@mcp.tool()
def wait_for_altitude(vehicle_id: str, target_altitude_m: float,
                      timeout_s: float = 5.0) -> dict:
    """Wait a few seconds for an aircraft to reach the altitude you asked for.

    Use this after takeoff instead of guessing whether the climb finished. Pass
    the target altitude the task called for, in metres above the launch point.

    You do not choose how close is close enough: this tool decides, and it
    requires the aircraft to be within one metre of the target. Reporting an
    altitude the aircraft has not reached is not acceptable, so do not describe
    a climb as finished until reached is true.

    timeout_s is a MAXIMUM, not a duration: this returns the instant the
    altitude is reached, so a generous timeout costs you nothing when the climb
    finishes early. It is capped at 20 and defaults to 5. Prefer a timeout that
    comfortably covers the climb — one call that waits 12 s beats three calls
    that wait 5 s each, and waited_s tells you what it actually took.

    reached=false is NOT a failure — read changed_m and advice, then call this
    tool again if it is still climbing. If changed_m is near zero the aircraft
    is not moving: check is_armed and flight_mode rather than waiting longer.
    """
    low = target_altitude_m - ALTITUDE_TOLERANCE_M
    high = target_altitude_m + ALTITUDE_TOLERANCE_M
    try:
        reached, first, last, waited, capped = _poll_until(
            vehicle_id, lambda t: low <= t["altitude_m"] <= high, timeout_s)
    except GatewayUnavailable as error:
        return _unavailable(error)

    changed = round(last["altitude_m"] - first["altitude_m"], 2)
    if reached:
        advice = (f"Within {ALTITUDE_TOLERANCE_M} m of {target_altitude_m} m. "
                  f"You may report this.")
    elif abs(changed) < STALLED_CHANGE_M:
        advice = (f"Not in range, and the altitude barely moved in "
                  f"{waited:.1f}s. The aircraft may not be climbing: check "
                  f"is_armed and flight_mode, and that a takeoff was accepted.")
    else:
        advice = (f"Not in range yet, but it moved {changed:+.2f} m. Call "
                  f"wait_for_altitude again to keep waiting.")

    return {
        "reached": reached,
        "altitude_m": last["altitude_m"],
        "target_altitude_m": target_altitude_m,
        "accepted_range_m": [round(low, 2), round(high, 2)],
        "changed_m": changed,
        "waited_s": round(waited, 1),
        "flight_mode": last["flight_mode"],
        "is_armed": last["is_armed"],
        "advice": advice,
    }


@mcp.tool()
def wait_for_position(vehicle_id: str, latitude_deg: float,
                      longitude_deg: float, timeout_s: float = 5.0) -> dict:
    """Wait a few seconds for an aircraft to arrive at a position.

    Use this after goto. An accepted goto means the setpoint was handed to the
    aircraft, not that it has flown there — a 200 m leg takes the better part of
    a minute, and nothing announces arrival.

    Pass the latitude and longitude you commanded. You do not choose how close
    counts as arrived: this tool requires the aircraft within 15 metres.

    timeout_s is a MAXIMUM, not a duration: it returns the instant the aircraft
    arrives, so a generous timeout costs nothing. Capped at 20 and defaults to
    5. arrived=false is not a failure — read closed_m, which says how much
    nearer it got, and call again if it is still moving.
    """
    try:
        arrived, first, last, waited, capped = _poll_until(
            vehicle_id,
            lambda t: _metres_between(t, latitude_deg, longitude_deg)
            <= ARRIVAL_TOLERANCE_M,
            timeout_s)
    except GatewayUnavailable as error:
        return _unavailable(error)

    started_away = _metres_between(first, latitude_deg, longitude_deg)
    still_away = _metres_between(last, latitude_deg, longitude_deg)
    closed = round(started_away - still_away, 1)
    if arrived:
        advice = f"Within {ARRIVAL_TOLERANCE_M} m of the target. You may report this."
    elif closed > 1.0:
        advice = (f"Not there yet, {still_away:.0f} m to run, but it closed "
                  f"{closed} m. Call wait_for_position again.")
    else:
        advice = (f"Not moving: still {still_away:.0f} m away after "
                  f"{waited:.1f}s. Check flight_mode and that a goto was "
                  f"accepted.")

    return {
        "arrived": arrived,
        "metres_to_target": round(still_away, 1),
        "closed_m": closed,
        "waited_s": round(waited, 1),
        "latitude_deg": last["latitude_deg"],
        "longitude_deg": last["longitude_deg"],
        "altitude_m": last["altitude_m"],
        "flight_mode": last["flight_mode"],
        "advice": advice,
    }


@mcp.tool()
def wait_for_disarm(vehicle_id: str, timeout_s: float = 5.0) -> dict:
    """Wait a few seconds for an aircraft to finish landing and disarm.

    A landing is only complete when the aircraft disarms itself, which happens a
    few seconds after it touches down. Use this after land or return_to_launch.

    timeout_s is a MAXIMUM, not a duration: this returns the instant the
    aircraft disarms, so a generous timeout costs nothing if it lands early. It
    is capped at 20 and defaults to 5. A full descent takes far longer than the
    cap, so expect several calls regardless; each one reports the current
    altitude so you can watch it come down.

    disarmed=true means the aircraft is down and the motors are off.
    """
    try:
        disarmed, first, last, waited, capped = _poll_until(
            vehicle_id, lambda t: not t["is_armed"], timeout_s)
    except GatewayUnavailable as error:
        return _unavailable(error)

    descended = round(first["altitude_m"] - last["altitude_m"], 2)
    if disarmed:
        advice = "The aircraft has landed and disarmed."
    elif descended > STALLED_CHANGE_M:
        advice = (f"Still airborne at {last['altitude_m']} m, descending "
                  f"({descended:+.2f} m). Call wait_for_disarm again.")
    else:
        advice = (f"Still armed at {last['altitude_m']} m and not descending. "
                  f"Check flight_mode: a landing needs LAND or RTL.")

    return {
        "disarmed": disarmed,
        "is_armed": last["is_armed"],
        "altitude_m": last["altitude_m"],
        "descended_m": descended,
        "waited_s": round(waited, 1),
        "flight_mode": last["flight_mode"],
        "advice": advice,
    }


# --- writes ----------------------------------------------------------------
#
# Every write returns the same shape:
#   op_id           the host-assigned id for this command; quote it in reports
#   status          "accepted" | "rejected" | "unknown"
#   reason          why, in terms you can act on
#   retryable       true if waiting and retrying could succeed
#   state_version   the vehicle's version after this command
#   reconciled      true if the outcome was recovered after a lost reply
#
# Constraints (geofence, altitude cap, arm-requires-approval) belong here, in
# the tool bodies, where a rejection can be structured and specific. They are
# not implemented yet.

def _guard(tool: str, args: dict):
    """Mission limits, checked before anything is sent.

    Returns a structured refusal, or None. Separate from _write so the check
    happens before a command is built, not after — and so the same rules can be
    called from the executor, which does not come through here.
    """
    telemetry = None
    fleet = {}
    try:
        fleet = client().fleet_snapshot()
        telemetry = fleet.get(args.get("vehicle_id"))
    except GatewayUnavailable:
        pass            # the rules that need it will simply not apply
    refusal = DEFAULT_CONSTRAINTS.check(tool, args, telemetry, fleet)
    return refusal.as_dict() if refusal else None


def _write(vehicle_id: str, action: str, **fields) -> dict:
    try:
        command = GatewayClient.build(vehicle_id, action, **fields)
        return client().send_command(command).as_dict()
    except GatewayUnavailable as error:
        return _unavailable(error)
    except ValueError as error:
        return {"error": "invalid_arguments", "reason": str(error)}


@mcp.tool()
def set_mode(vehicle_id: str, mode_name: str) -> dict:
    """Change an aircraft's flight mode.

    GUIDED is the mode that accepts commanded takeoffs and positions; a takeoff
    or goto in any other mode is rejected. LOITER holds position. RTL returns to
    launch and lands. Rejected as retryable if the vehicle does not reach the
    mode within 3 s, which usually means it is not ready yet.
    """
    blocked = _guard("set_mode", {"vehicle_id": vehicle_id,
                                  "mode_name": mode_name})
    return blocked or _write(vehicle_id, "set_mode", mode_name=mode_name)


@mcp.tool()
def arm(vehicle_id: str) -> dict:
    """Arm an aircraft's motors. Required before takeoff.

    Commonly rejected as retryable shortly after startup, because the aircraft
    refuses to arm until its position estimate has settled — this can take 30 to
    90 s. A retryable rejection here means wait a few seconds and call arm
    again; it does not mean the vehicle is broken.

    Acceptance is confirmed against telemetry, so an accepted arm means the
    aircraft really is armed. Take off promptly afterwards: an armed aircraft
    left sitting on the ground disarms itself again after about 10 s.
    """
    blocked = _guard("arm", {"vehicle_id": vehicle_id})
    return blocked or _write(vehicle_id, "arm")


@mcp.tool()
def disarm(vehicle_id: str) -> dict:
    """Disarm an aircraft's motors, on the ground.

    The aircraft refuses this while it is flying, which is a retryable
    rejection — it does not become possible by asking again in the air. To bring
    a flying aircraft down, use land or return_to_launch and wait for telemetry
    to report is_armed false; it disarms itself once it has settled.
    """
    return _write(vehicle_id, "disarm")


@mcp.tool()
def takeoff(vehicle_id: str, target_altitude_m: float) -> dict:
    """Take off from the ground to an altitude above the launch point, in metres.

    ONLY valid while the aircraft is on the ground. If it is already flying,
    this is rejected permanently — to change altitude in flight, call goto with
    the aircraft's current latitude and longitude and the new altitude. Check
    altitude_m in get_telemetry before planning a takeoff.

    Requires the vehicle to be armed and in GUIDED mode; both are retryable
    rejections naming which one failed. target_altitude_m must be greater than
    0; practical range is 2 to 100 m.

    Acceptance means the climb has started, NOT that the altitude was reached.
    Poll get_telemetry until altitude_m is near the target before continuing.
    """
    blocked = _guard("takeoff", {"vehicle_id": vehicle_id,
                                 "target_altitude_m": target_altitude_m})
    return blocked or _write(vehicle_id, "takeoff",
                             target_altitude_m=target_altitude_m)


@mcp.tool()
def goto(vehicle_id: str, latitude_deg: float, longitude_deg: float,
         altitude_m: float) -> dict:
    """Fly to a position: plain degrees WGS-84, altitude in metres above launch.

    This is also how you change altitude in flight: pass the aircraft's current
    latitude and longitude with a new altitude_m and it will climb or descend
    in place. takeoff does not work once airborne.

    Requires the vehicle to be armed, flying and in GUIDED mode.

    This command has no acknowledgement in the underlying protocol, so
    "accepted" means the setpoint was handed to the aircraft. Only telemetry
    confirms it is actually flying there — poll get_telemetry and watch the
    position close on the target.
    """
    blocked = _guard("goto", {"vehicle_id": vehicle_id,
                              "latitude_deg": latitude_deg,
                              "longitude_deg": longitude_deg,
                              "altitude_m": altitude_m})
    return blocked or _write(vehicle_id, "goto_position",
                             latitude_deg=latitude_deg,
                             longitude_deg=longitude_deg,
                             altitude_m=altitude_m)


@mcp.tool()
def return_to_launch(vehicle_id: str) -> dict:
    """Return to the launch point and land there.

    Acceptance means the vehicle has entered RTL, not that it has landed. Watch
    telemetry until is_armed is false.
    """
    return _write(vehicle_id, "return_to_launch")


@mcp.tool()
def land(vehicle_id: str) -> dict:
    """Descend and land at the current position.

    Acceptance means the descent has begun. Watch telemetry until is_armed is
    false to know the landing finished.
    """
    return _write(vehicle_id, "land")


if __name__ == "__main__":
    # stdio carries the protocol on stdout, so anything we say goes to stderr.
    print("vehicle-gateway MCP server on stdio", file=sys.stderr)
    # Start listening before serving, so the first read is not racing the
    # first 5 Hz telemetry sample.
    client().await_first_sample()
    mcp.run(transport="stdio")
