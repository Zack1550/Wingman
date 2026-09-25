"""Fixtures shared by the harness tests.

Everything here runs against the in-process fake gateway: no Docker, no SITL,
no aircraft. A layer-one test should fail for one reason only.
"""
import time

import pytest

from harness.store import MissionStore
from mcp_server.tests.fake_gateway import FakeGateway, free_udp_port
from vehicle_gateway.client import GatewayClient

RUN = "run-test"


@pytest.fixture
def store(tmp_path):
    with MissionStore(tmp_path / "mission.sqlite3") as opened:
        yield opened


@pytest.fixture
def gateway():
    port = free_udp_port()
    started = FakeGateway(telemetry_port=port).start()
    started._telemetry_port_for_test = port
    yield started
    started.stop()


def ready_client(gateway, port, ack_timeout_s=1.0):
    """Wait for EVERY vehicle, not just the first datagram.

    Telemetry is eventually consistent and the fake sends one vehicle per
    datagram, so a client that stops waiting at the first sample can start with
    a half-populated roster — and then insist the vehicle under test does not
    exist.
    """
    made = GatewayClient(command_port=gateway.command_port,
                         telemetry_port=port,
                         ack_timeout_s=ack_timeout_s).start()
    deadline = time.time() + 3
    while time.time() < deadline:
        if len(made.known_vehicles()) >= len(gateway.vehicles):
            break
        time.sleep(0.02)
    return made


@pytest.fixture
def client(gateway):
    made = ready_client(gateway, gateway._telemetry_port_for_test)
    yield made
    made.close()
