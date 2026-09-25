#!/usr/bin/env python3
"""First scripted flight against ArduPilot SITL.

Connects to one simulated copter, requests telemetry streams, waits for the
EKF to produce a position estimate, arms, takes off, flies one waypoint,
returns to launch, and waits for disarm so the vehicle is left clean for the
next run.
"""
import time
from pymavlink import mavutil

VEHICLE_CONNECTION_STRING = 'tcp:127.0.0.1:5760'   # your router's exposed port
TAKEOFF_ALTITUDE_M = 10.0
WAYPOINT_DISTANCE_NORTH_M = 50.0

# MAVLink encodes latitude and longitude as integers scaled by 1e7.
DEGREES_TO_MAVLINK_INT = 1e7
METRES_PER_DEGREE_LATITUDE = 111_000

MAV_RESULT_NAMES = {
    0: "ACCEPTED", 1: "TEMPORARILY_REJECTED", 2: "DENIED",
    3: "UNSUPPORTED", 4: "FAILED", 5: "IN_PROGRESS",
}

# Everything the message reader learns about the vehicle lives here.
vehicle_state = {
    "altitude_m": None,        # None until the first position message arrives
    "latitude_e7": None,
    "longitude_e7": None,
    "gps_fix_type": 0,         # 3 or higher means a usable 3D fix
    "flight_mode": "?",
    "is_armed": False,
    "command_acks": {},        # MAV_CMD id -> MAV_RESULT code
}

vehicle_link = mavutil.mavlink_connection(VEHICLE_CONNECTION_STRING)
print(f"waiting for heartbeat on {VEHICLE_CONNECTION_STRING} ...")
vehicle_link.wait_heartbeat()
print(f"connected to system {vehicle_link.target_system}, "
      f"component {vehicle_link.target_component}")

# ArduPilot streams telemetry only at rates a ground station explicitly asks
# for, so without this request no position messages ever arrive.
vehicle_link.mav.request_data_stream_send(
    vehicle_link.target_system, vehicle_link.target_component,
    mavutil.mavlink.MAV_DATA_STREAM_ALL, 4, 1)     # 4 Hz, 1 = start streaming


def process_messages_for(duration_seconds, phase_label=""):
    """Read incoming MAVLink for a while, updating vehicle_state as we go.

    Status text and command acks print immediately; a one-line state summary
    prints about once per second so a long wait stays legible.
    """
    stop_at = time.time() + duration_seconds
    last_summary_at = 0.0
    while time.time() < stop_at:
        message = vehicle_link.recv_match(blocking=True, timeout=0.5)
        if message is None:
            continue
        if message.get_srcSystem() != vehicle_link.target_system:
            continue               # a different copter sharing the same router
        message_type = message.get_type()

        if message_type == 'STATUSTEXT':
            print("   [status]", message.text)
        elif message_type == 'COMMAND_ACK':
            vehicle_state["command_acks"][message.command] = message.result
            result_name = MAV_RESULT_NAMES.get(message.result, "UNKNOWN")
            print(f"   [ack] command={message.command} "
                  f"result={message.result} ({result_name})")
        elif message_type == 'GLOBAL_POSITION_INT':
            vehicle_state["altitude_m"] = message.relative_alt / 1000.0
            vehicle_state["latitude_e7"] = message.lat
            vehicle_state["longitude_e7"] = message.lon
        elif message_type == 'GPS_RAW_INT':
            vehicle_state["gps_fix_type"] = message.fix_type
        elif message_type == 'HEARTBEAT':
            vehicle_state["flight_mode"] = mavutil.mode_string_v10(message)
            vehicle_state["is_armed"] = bool(
                message.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)

        if time.time() - last_summary_at >= 1.0:
            last_summary_at = time.time()
            altitude_m = vehicle_state["altitude_m"]
            altitude_text = "  --  " if altitude_m is None else f"{altitude_m:6.1f}"
            print(f"{phase_label:10s} mode={vehicle_state['flight_mode']:9s} "
                  f"armed={str(vehicle_state['is_armed']):5s} "
                  f"fix={vehicle_state['gps_fix_type']} "
                  f"alt={altitude_text}m")


def wait_until(condition, timeout_seconds, phase_label):
    """Keep reading messages until condition() is true or time runs out."""
    stop_at = time.time() + timeout_seconds
    while time.time() < stop_at:
        process_messages_for(1.0, phase_label)
        if condition():
            return True
    return False


def send_command_and_await_ack(command_id, *params,
                               timeout_seconds=5.0, phase_label=""):
    """Send a COMMAND_LONG and wait for its ack. Returns the MAV_RESULT code,
    or None if no ack arrived before the timeout."""
    vehicle_state["command_acks"].pop(command_id, None)
    padded_params = list(params) + [0.0] * (7 - len(params))
    vehicle_link.mav.command_long_send(
        vehicle_link.target_system, vehicle_link.target_component,
        command_id, 0, *padded_params)
    wait_until(lambda: command_id in vehicle_state["command_acks"],
               timeout_seconds, phase_label)
    return vehicle_state["command_acks"].get(command_id)


def arm_with_retries(timeout_seconds=120):
    """Arm the vehicle, retrying until it takes.

    Pre-arm checks reject arming until the EKF has settled on a position
    estimate, which takes tens of seconds after boot. A single attempt
    usually loses that race.
    """
    stop_at = time.time() + timeout_seconds
    attempt_number = 0
    while time.time() < stop_at:
        attempt_number += 1
        arm_result = send_command_and_await_ack(
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 1,
            phase_label=f"arm #{attempt_number}")
        if wait_until(lambda: vehicle_state["is_armed"], 3,
                      f"arm #{attempt_number}"):
            return True
        result_name = MAV_RESULT_NAMES.get(arm_result, "NO_ACK")
        print(f"   arm attempt {attempt_number} -> {result_name}, retrying")
    return False


# --- preflight checks ------------------------------------------------------
process_messages_for(4, "connect")

if vehicle_state["altitude_m"] is None:
    print("\nNo GLOBAL_POSITION_INT arrived. Either the stream request was "
          "ignored or the vehicle has no position estimate yet.")
    raise SystemExit(1)

if vehicle_state["is_armed"]:
    print("\nVehicle is already armed — stale state from a previous run.\n"
          "  docker compose -f docker-compose-2.yml down\n"
          "  docker compose -f docker-compose-2.yml up -d")
    raise SystemExit(1)

home_latitude_e7 = vehicle_state["latitude_e7"]
home_longitude_e7 = vehicle_state["longitude_e7"]
print(f"home at {home_latitude_e7 / DEGREES_TO_MAVLINK_INT:.6f}, "
      f"{home_longitude_e7 / DEGREES_TO_MAVLINK_INT:.6f}")

# --- fly the mission -------------------------------------------------------
vehicle_link.set_mode('GUIDED')
if not wait_until(lambda: vehicle_state["flight_mode"] == 'GUIDED',
                  10, "guided"):
    print("never entered GUIDED mode")
    raise SystemExit(1)

if not arm_with_retries():
    print("\nnever armed — see the [status] lines above for the reason")
    raise SystemExit(1)
print("ARMED")

takeoff_result = send_command_and_await_ack(
    mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
    0, 0, 0, 0, 0, 0, TAKEOFF_ALTITUDE_M,        # param7 is target altitude
    phase_label="takeoff")
if takeoff_result != 0:
    print(f"\ntakeoff rejected: "
          f"{MAV_RESULT_NAMES.get(takeoff_result, takeoff_result)}")
    raise SystemExit(1)

reached_takeoff_altitude = wait_until(
    lambda: vehicle_state["altitude_m"] is not None
    and vehicle_state["altitude_m"] >= TAKEOFF_ALTITUDE_M * 0.9,
    60, "climbing")
if not reached_takeoff_altitude:
    print("did not reach takeoff altitude")
    raise SystemExit(1)
print(f"holding at {vehicle_state['altitude_m']:.1f}m")

northward_offset_e7 = int(
    WAYPOINT_DISTANCE_NORTH_M / METRES_PER_DEGREE_LATITUDE
    * DEGREES_TO_MAVLINK_INT)
target_latitude_e7 = home_latitude_e7 + northward_offset_e7

IGNORE_ALL_BUT_POSITION = 0b0000111111111000   # velocity/accel/yaw unused
vehicle_link.mav.set_position_target_global_int_send(
    0,                                          # timestamp, unused here
    vehicle_link.target_system, vehicle_link.target_component,
    mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
    IGNORE_ALL_BUT_POSITION,
    target_latitude_e7, home_longitude_e7, TAKEOFF_ALTITUDE_M,
    0, 0, 0,                                    # velocity x, y, z
    0, 0, 0,                                    # acceleration x, y, z
    0, 0)                                       # yaw, yaw rate
print(f"flying to {target_latitude_e7 / DEGREES_TO_MAVLINK_INT:.6f}, "
      f"{home_longitude_e7 / DEGREES_TO_MAVLINK_INT:.6f}")
process_messages_for(30, "transit")

# --- return home and leave the vehicle clean -------------------------------
vehicle_link.set_mode('RTL')
if wait_until(lambda: not vehicle_state["is_armed"], 120, "rtl"):
    print("landed and disarmed — vehicle left in a clean state")
else:
    print("still armed after RTL; the next run will need a stack reset")