#!/usr/bin/env python3
"""A hand client for the gateway — the smallest thing that proves it works.

Not the harness and not the MCP server; just enough to see a command go out, an
ack come back, and telemetry stream underneath. Its one interesting behaviour is
what it does when an ack never arrives: it asks the gateway what happened
instead of retrying blindly or reporting a failure it cannot support.

    python probe.py telemetry            # watch the 5 Hz broadcast
    python probe.py arm
    python probe.py set_mode GUIDED
    python probe.py takeoff 15
    python probe.py goto 51.5018 -2.5518 15
    python probe.py rtl
"""
import argparse
import socket
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "generated"))
import vehicle_pb2                                             # noqa: E402

ACK_TIMEOUT_S = 3.0


def watch_telemetry(port, seconds):
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('0.0.0.0', port))
    listener.settimeout(1.0)
    stop_at = time.time() + seconds
    while time.time() < stop_at:
        try:
            datagram, _ = listener.recvfrom(65535)
        except socket.timeout:
            print("no telemetry — is the gateway running?")
            continue
        t = vehicle_pb2.VehicleTelemetry()
        t.ParseFromString(datagram)
        print(f"{t.vehicle_id:9s} v{t.state_version:<3d} "
              f"mode={t.flight_mode:9s} armed={str(t.is_armed):5s} "
              f"alt={t.altitude_m:6.1f}m fix={t.gps_fix_type} "
              f"{t.latitude_deg:.6f},{t.longitude_deg:.6f}")


def build_command(action, values, vehicle_id):
    command = vehicle_pb2.VehicleCommand(
        operation_id=f"op-{uuid.uuid4().hex[:8]}",
        vehicle_id=vehicle_id,
        issued_at_unix_ms=int(time.time() * 1000),
    )
    if action == 'arm':
        command.arm.SetInParent()
    elif action == 'disarm':
        command.disarm.SetInParent()
    elif action == 'rtl':
        command.return_to_launch.SetInParent()
    elif action == 'land':
        command.land.SetInParent()
    elif action == 'set_mode':
        command.set_mode.mode_name = values[0]
    elif action == 'takeoff':
        command.takeoff.target_altitude_m = float(values[0])
    elif action == 'goto':
        command.goto_position.target_latitude_deg = float(values[0])
        command.goto_position.target_longitude_deg = float(values[1])
        command.goto_position.target_altitude_m = float(values[2])
    else:
        raise SystemExit(f"unknown action '{action}'")
    return command


def send_and_reconcile(command, host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(ACK_TIMEOUT_S)

    request = vehicle_pb2.ClientRequest()
    request.command.CopyFrom(command)
    print(f"-> {command.operation_id} {command.WhichOneof('action')} "
          f"to {command.vehicle_id}")
    sock.sendto(request.SerializeToString(), (host, port))

    try:
        datagram, _ = sock.recvfrom(65535)
    except socket.timeout:
        # The command may have applied. Ask, do not assume, and do not resend a
        # fresh operation_id — that would be a second command, not a retry.
        print(f"!! no ack in {ACK_TIMEOUT_S}s — outcome UNKNOWN, reconciling")
        return reconcile(sock, command.operation_id, host, port)

    response = vehicle_pb2.GatewayResponse()
    response.ParseFromString(datagram)
    print_ack(response.ack)
    return response.ack


def reconcile(sock, operation_id, host, port):
    query = vehicle_pb2.ClientRequest()
    query.status_query.operation_id = operation_id
    sock.sendto(query.SerializeToString(), (host, port))
    try:
        datagram, _ = sock.recvfrom(65535)
    except socket.timeout:
        print("?? gateway did not answer the status query either — "
              "still UNKNOWN, do not assume either outcome")
        return None

    response = vehicle_pb2.GatewayResponse()
    response.ParseFromString(datagram)
    if not response.status.is_known:
        print("<- gateway never saw it: the command never landed, safe to resend")
        return None
    print("<- reconciled from the gateway ledger:")
    print_ack(response.status.ack)
    return response.status.ack


def print_ack(ack):
    print(f"<- {vehicle_pb2.CommandStatus.Name(ack.status)} "
          f"state_version={ack.state_version}  {ack.reason}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action')
    parser.add_argument('values', nargs='*')
    parser.add_argument('--vehicle', default='copter_1')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--command-port', type=int, default=14550)
    parser.add_argument('--telemetry-port', type=int, default=14551)
    parser.add_argument('--seconds', type=float, default=10.0,
                        help='how long "telemetry" watches for')
    args = parser.parse_args()

    if args.action == 'telemetry':
        watch_telemetry(args.telemetry_port, args.seconds)
        return
    command = build_command(args.action, args.values, args.vehicle)
    send_and_reconcile(command, args.host, args.command_port)


if __name__ == '__main__':
    main()
