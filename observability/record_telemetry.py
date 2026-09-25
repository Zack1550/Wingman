#!/usr/bin/env python3
"""Capture the telemetry broadcast to a file, with arrival times.

    python -m observability.record_telemetry --out captures/mission.jsonl

Telemetry is broadcast and otherwise discarded — the traces record what the
agent decided, never what the aircraft was doing while it decided. Without this
half there is no timeline to scrub, only a list of decisions.

Written as JSONL rather than straight to MCAP so the log can be rebuilt with a
different schema without flying the mission again.
"""
import argparse
import json
import signal
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vehicle_gateway" / "generated"))
import vehicle_pb2                                             # noqa: E402

DEFAULT_PORT = 14553          # the recorder's own address in the fan-out


def record(port, out_path, stop_after_s=None):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", port))
    listener.settimeout(1.0)

    running = {"go": True}

    def stop(*_):
        running["go"] = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    deadline = time.time() + stop_after_s if stop_after_s else None
    count = 0
    with out_path.open("w", encoding="utf-8") as handle:
        print(f"recording udp/{port} -> {out_path}", file=sys.stderr)
        while running["go"] and (deadline is None or time.time() < deadline):
            try:
                datagram, _ = listener.recvfrom(65535)
            except socket.timeout:
                continue
            sample = vehicle_pb2.VehicleTelemetry()
            try:
                sample.ParseFromString(datagram)
            except Exception:
                continue
            handle.write(json.dumps({
                # Arrival time, because that is what shares a clock with the
                # agent trace. The vehicle's own sampled_at is kept alongside
                # so the difference stays visible.
                "received_at_unix_ms": int(time.time() * 1000),
                "sampled_at_unix_ms": sample.sampled_at_unix_ms,
                "vehicle_id": sample.vehicle_id,
                "latitude_deg": sample.latitude_deg,
                "longitude_deg": sample.longitude_deg,
                "altitude_m": sample.altitude_m,
                "ground_speed_mps": sample.ground_speed_mps,
                "heading_deg": sample.heading_deg,
                "flight_mode": sample.flight_mode,
                "is_armed": sample.is_armed,
                "gps_fix_type": sample.gps_fix_type,
                "battery_percent": sample.battery_percent,
                "state_version": sample.state_version,
            }) + "\n")
            handle.flush()
            count += 1
    listener.close()
    print(f"recorded {count} samples", file=sys.stderr)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--out", required=True)
    parser.add_argument("--seconds", type=float, default=None)
    args = parser.parse_args()
    record(args.port, args.out, args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
