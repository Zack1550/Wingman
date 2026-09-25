"""Shared fixtures.

Layer one (everything here except the `live` marker) runs against an in-process
fake gateway: no Docker, no SITL, no aircraft. It proves the harness.

Layer two is marked `live` and skips itself unless a real gateway answers on
udp/14550. It measures the stack end to end, and it is allowed to be slow.

    pytest                            # layer one only, seconds
    pytest -m live                    # layer two, needs SITL and gateway.py
    pytest -m "live or not live"      # both
"""
import socket
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from mcp_server.tests.fake_gateway import FakeGateway, free_udp_port  # noqa: E402
from vehicle_gateway.client import GatewayClient                      # noqa: E402

LIVE_COMMAND_PORT = 14550
LIVE_TELEMETRY_PORT = 14551


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live: needs a real gateway and SITL running")


def pytest_collection_modifyitems(config, items):
    """Skip live tests by default; -m live opts in."""
    if config.getoption("-m"):
        return
    skip = pytest.mark.skip(reason="needs a live gateway; run with -m live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


def _gateway_is_up(timeout_s=1.0):
    """Ask the real gateway a harmless question and see if anything answers."""
    sys.path.insert(0, str(REPO_ROOT / "vehicle_gateway" / "generated"))
    import vehicle_pb2
    query = vehicle_pb2.ClientRequest()
    query.status_query.operation_id = "probe-is-anyone-there"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout_s)
        try:
            sock.sendto(query.SerializeToString(),
                        ("127.0.0.1", LIVE_COMMAND_PORT))
            sock.recvfrom(65535)
            return True
        except (socket.timeout, OSError):
            return False


@pytest.fixture
def telemetry_port():
    return free_udp_port()


@pytest.fixture
def fake_gateway(telemetry_port):
    gateway = FakeGateway(telemetry_port=telemetry_port).start()
    yield gateway
    gateway.stop()


@pytest.fixture
def make_client(telemetry_port):
    """Build a GatewayClient pointed at whichever fake the test started."""
    created = []

    def factory(gateway=None, command_port=None, **kwargs):
        client = GatewayClient(
            command_port=command_port or gateway.command_port,
            telemetry_port=telemetry_port,
            **kwargs,
        ).start()
        created.append(client)
        if gateway is not None:
            # Waiting for the first datagram is not enough: it carries one
            # vehicle, and a test that reads immediately afterwards sees a
            # half-populated roster. Telemetry is eventually consistent.
            deadline = time.time() + 3.0
            while time.time() < deadline:
                if len(client.known_vehicles()) >= len(gateway.vehicles):
                    break
                time.sleep(0.02)
        return client

    yield factory
    for client in created:
        client.close()


@pytest.fixture
def client(fake_gateway, make_client):
    return make_client(fake_gateway)


@pytest.fixture(scope="session")
def live_client():
    if not _gateway_is_up():
        pytest.skip("no gateway answering on udp/14550")
    client = GatewayClient(command_port=LIVE_COMMAND_PORT,
                           telemetry_port=LIVE_TELEMETRY_PORT).start()
    client.await_first_sample(timeout_s=3.0)
    yield client
    client.close()
