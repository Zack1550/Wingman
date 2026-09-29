"""A MAVLink router with one ArduCopter behind it, on a real TCP port.

FakeGateway stands in for the gateway; this stands in for what the gateway talks
to, so the real vehicle_gateway/gateway.py can run as a process in a test and be
killed, restarted, and cut off from its vehicle. It speaks real MAVLink through
pymavlink and copies the ArduPilot behaviour the gateway depends on:

- a HEARTBEAT at 10 Hz carrying the mode and the armed flag, always;
- position and battery only after a REQUEST_DATA_STREAM, as ArduPilot streams
  nothing until a ground station asks. A gateway that reconnects without asking
  again gets heartbeats and no position, and a test can see that;
- a COMMAND_ACK for every COMMAND_LONG, and arming that shows up in the next
  heartbeat.

stop() closes the listener and every connection, which is what a router or
simulator that dies looks like from the gateway's side. start() again on the
same port brings it back, with a new `generation` so a test can tell telemetry
from after the restart from telemetry before it.
"""
import random
import socket
import threading
import time

from pymavlink.dialects.v20 import ardupilotmega as mavlink

GUIDED = 4                        # ArduCopter custom_mode number
HOME_LAT, HOME_LON = 51.501592, -2.551791
SEND_HZ = 10.0


def free_tcp_port():
    """A free port below the ephemeral range, never one the OS might pick.

    While the fake router is down the gateway keeps dialling its port, and
    each attempt takes an ephemeral source port. If the router's port is one
    of those, an attempt can be given that very port and Linux connects the
    socket to itself, which then holds the port the router wants back. The
    real router's 5760 is outside the range; this keeps the fake's outside
    it too.
    """
    for port in random.sample(range(20000, 32000), 200):
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            probe.close()
    raise RuntimeError("no free TCP port found below the ephemeral range")


class FakeVehicle:

    def __init__(self, port, system_id=1):
        self.port = port
        self.system_id = system_id
        self.mode = GUIDED
        self.armed = False
        self.altitude_m = 0.0
        self.generation = 0
        self.commands = []              # every COMMAND_LONG id, in order
        self._lock = threading.Lock()
        self._running = False
        self._server = None
        self._connections = []
        self._threads = []

    # -- what a test asserts on ----------------------------------------------

    def count(self, command_id):
        with self._lock:
            return self.commands.count(command_id)

    def latitude_for(self, generation):
        """Each generation reports a slightly different position."""
        return HOME_LAT + generation * 0.001

    # -- lifecycle -------------------------------------------------------------

    def start(self):
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", self.port))
        server.listen()
        server.settimeout(0.2)
        self._server = server
        self._running = True
        self.generation += 1
        self._threads = [threading.Thread(target=self._accept, args=(server,),
                                          daemon=True)]
        self._threads[0].start()
        return self

    def stop(self):
        """Gone, and the port free again by the time this returns.

        Closing the listener is not enough on its own: a thread still inside
        accept() keeps it alive for a moment, and a test that starts the
        router again straight away would find the port taken.
        """
        self._running = False
        with self._lock:
            connections, self._connections = self._connections, []
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        for thread in self._threads:
            thread.join(timeout=2)
        if self._server is not None:
            self._server.close()
            self._server = None

    # -- serving -----------------------------------------------------------------

    def _accept(self, server):
        while self._running:
            try:
                connection, _ = server.accept()
            except (socket.timeout, OSError):
                continue
            if not self._running:
                connection.close()
                return
            with self._lock:
                self._connections.append(connection)
            thread = threading.Thread(target=self._serve, args=(connection,),
                                      daemon=True)
            self._threads.append(thread)
            thread.start()

    def _serve(self, connection):
        encoder = mavlink.MAVLink(None, srcSystem=self.system_id, srcComponent=1)
        parser = mavlink.MAVLink(None)
        connection.settimeout(0.02)
        streaming = False
        next_send = 0.0
        generation = self.generation
        while self._running:
            now = time.time()
            if now >= next_send:
                next_send = now + 1.0 / SEND_HZ
                try:
                    for message in self._outgoing(encoder, streaming, generation):
                        connection.sendall(message.pack(encoder))
                except OSError:
                    return
            try:
                data = connection.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            for message in parser.parse_buffer(data) or []:
                if message.get_type() == "REQUEST_DATA_STREAM":
                    streaming = True
                else:
                    reply = self._handle(encoder, message)
                    if reply is not None:
                        try:
                            connection.sendall(reply.pack(encoder))
                        except OSError:
                            return

    def _outgoing(self, encoder, streaming, generation):
        base_mode = mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
        if self.armed:
            base_mode |= mavlink.MAV_MODE_FLAG_SAFETY_ARMED
        yield encoder.heartbeat_encode(
            mavlink.MAV_TYPE_QUADROTOR, mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
            base_mode, self.mode, mavlink.MAV_STATE_ACTIVE)
        if streaming:
            yield encoder.global_position_int_encode(
                int(time.time() * 1000) & 0xFFFFFFFF,
                int(self.latitude_for(generation) * 1e7), int(HOME_LON * 1e7),
                int(self.altitude_m * 1000), int(self.altitude_m * 1000),
                0, 0, 0, 0)
            yield encoder.sys_status_encode(0, 0, 0, 0, 12600, 0, 100,
                                            0, 0, 0, 0, 0, 0)

    def _handle(self, encoder, message):
        kind = message.get_type()
        if kind == "SET_MODE":
            self.mode = message.custom_mode
            return None
        if kind != "COMMAND_LONG":
            return None
        with self._lock:
            self.commands.append(message.command)
        if message.command == mavlink.MAV_CMD_COMPONENT_ARM_DISARM:
            self.armed = message.param1 == 1
        elif message.command == mavlink.MAV_CMD_NAV_TAKEOFF:
            self.altitude_m = message.param7
        return encoder.command_ack_encode(message.command,
                                          mavlink.MAV_RESULT_ACCEPTED)
