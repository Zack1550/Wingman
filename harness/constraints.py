"""Limits that nobody in this loop can authorise away.

An approval and a constraint answer different questions. An approval asks "do
we want this, here, now?" and a human decides. A constraint says "this is not
permitted" and no one in the loop decides — not the model, not the operator
clicking yes. A geofence is not a prompt.

Two consequences shape this file:

  Checked BEFORE the operator is asked.  Putting a refusable command in front of
  a human first teaches them that the prompt is noise. If the system will not do
  it, say so and never raise the question.

  Called from every path that can command.  The MCP tools are the model's way in
  and the executor is the approved-plan way in, and they do not share a code
  path to the vehicle. A fence enforced in only one of them fences only one of
  them.

The gateway deliberately does not enforce these. It is transport and
translation — the piece meant to be swappable for a real vehicle's API — and
mission limits sit above it. On real hardware the fence would also be enforced
onboard, which is defence in depth rather than a reason to skip it here.
"""
from dataclasses import dataclass, field

# A box around the ArduPilot SITL default home at Bristol, roughly 400 m across.
# Deliberately small: a fence you cannot hit in testing is a fence you have not
# tested.
DEFAULT_FENCE = [
    (51.49980, -2.55380),
    (51.49980, -2.54980),
    (51.50340, -2.54980),
    (51.50340, -2.55380),
]
DEFAULT_MAX_ALTITUDE_M = 60.0
DEFAULT_MIN_ALTITUDE_M = 0.0

# A protected cylinder around each aircraft, not a sphere: two vehicles may sit
# above one another with little horizontal gap, which is normal and safe, but
# they may not be close in both axes at once. The horizontal figure is small on
# purpose — copter_1 and copter_2 park 13.1 m apart in this stack, and a limit
# above that would refuse every command before either aircraft left the ground.
DEFAULT_MIN_SEPARATION_M = 8.0
DEFAULT_MIN_VERTICAL_M = 4.0


def horizontal_metres(lat_a, lon_a, lat_b, lon_b):
    """Flat-earth, and adequate: these are hundreds of metres, not hundreds of km."""
    north = (lat_a - lat_b) * 111_000
    east = (lon_a - lon_b) * 111_000 * 0.62
    return (north ** 2 + east ** 2) ** 0.5


@dataclass
class Refusal:
    """Why a command is not permitted, in terms the caller can act on."""
    rule: str                  # geofence | altitude_cap | altitude_floor
    reason: str
    limit: object = None
    offered: object = None

    def as_dict(self):
        return {
            "error": "constraint_violation",
            "rule": self.rule,
            "reason": self.reason,
            "limit": self.limit,
            "you_asked_for": self.offered,
            "advice": "This limit is enforced in code and cannot be approved "
                      "or overridden from here. Change the plan.",
        }


def point_in_polygon(latitude, longitude, polygon):
    """Ray casting. A point exactly on an edge counts as inside.

    Boundary handling is a decision, not an accident: a waypoint placed on the
    fence line should not depend on floating-point luck for whether it flies.
    """
    inside = False
    count = len(polygon)
    for index in range(count):
        lat_a, lon_a = polygon[index]
        lat_b, lon_b = polygon[(index + 1) % count]

        # On this edge?
        if min(lat_a, lat_b) - 1e-12 <= latitude <= max(lat_a, lat_b) + 1e-12 and \
           min(lon_a, lon_b) - 1e-12 <= longitude <= max(lon_a, lon_b) + 1e-12:
            cross = ((lon_b - lon_a) * (latitude - lat_a)
                     - (lat_b - lat_a) * (longitude - lon_a))
            if abs(cross) < 1e-12:
                return True

        if (lat_a > latitude) != (lat_b > latitude):
            crossing_lon = (lon_b - lon_a) * (latitude - lat_a) / \
                           (lat_b - lat_a) + lon_a
            if longitude < crossing_lon:
                inside = not inside
    return inside


@dataclass
class Constraints:
    fence: list = field(default_factory=lambda: list(DEFAULT_FENCE))
    max_altitude_m: float = DEFAULT_MAX_ALTITUDE_M
    min_altitude_m: float = DEFAULT_MIN_ALTITUDE_M
    min_separation_m: float = DEFAULT_MIN_SEPARATION_M
    min_vertical_m: float = DEFAULT_MIN_VERTICAL_M

    # -- individual rules ---------------------------------------------------

    def check_altitude(self, altitude_m):
        if altitude_m is None:
            return None
        if altitude_m > self.max_altitude_m:
            return Refusal(
                rule="altitude_cap",
                reason=(f"{altitude_m:g} m is above the {self.max_altitude_m:g} m "
                        f"ceiling for this mission"),
                limit=self.max_altitude_m, offered=altitude_m)
        if altitude_m < self.min_altitude_m:
            return Refusal(
                rule="altitude_floor",
                reason=(f"{altitude_m:g} m is below the "
                        f"{self.min_altitude_m:g} m floor"),
                limit=self.min_altitude_m, offered=altitude_m)
        return None

    def check_position(self, latitude_deg, longitude_deg):
        if latitude_deg is None or longitude_deg is None:
            return None
        if not point_in_polygon(latitude_deg, longitude_deg, self.fence):
            return Refusal(
                rule="geofence",
                reason=(f"{latitude_deg:.6f}, {longitude_deg:.6f} is outside "
                        f"the operating area"),
                limit=self.fence,
                offered=[latitude_deg, longitude_deg])
        return None

    def check_separation(self, vehicle_id, latitude, longitude, altitude, fleet):
        """Would this put the aircraft inside another one's protected cylinder?

        The first rule here that is not decidable from one aircraft's command.
        A fence and a ceiling are properties of a single vehicle; separation is
        a property of the fleet, and the check needs to see the others. That is
        why `check` grew a `fleet` argument rather than this living in the same
        shape as the rest.
        """
        if latitude is None or not fleet:
            return None
        for other_id, other in fleet.items():
            if other_id == vehicle_id or not other:
                continue
            gap = horizontal_metres(latitude, longitude,
                                    other.get("latitude_deg", 0),
                                    other.get("longitude_deg", 0))
            vertical = abs((altitude if altitude is not None else 0)
                           - other.get("altitude_m", 0))
            if gap < self.min_separation_m and vertical < self.min_vertical_m:
                return Refusal(
                    rule="separation",
                    reason=(f"that point is {gap:.1f} m from {other_id} with "
                            f"only {vertical:.1f} m of height between them; "
                            f"the fleet requires {self.min_separation_m:g} m "
                            f"horizontally or {self.min_vertical_m:g} m "
                            f"vertically"),
                    limit=[self.min_separation_m, self.min_vertical_m],
                    offered=[round(gap, 1), round(vertical, 1)])
        return None

    # -- the one entry point ------------------------------------------------

    def check(self, tool, args, telemetry=None, fleet=None):
        """Return a Refusal, or None if this command is permitted.

        telemetry is where this aircraft is; fleet is where all of them are.
        Rules that depend only on the command need neither, rules about the
        vehicle's own state need the first, and fleet rules need the second.
        """
        args = args or {}
        vehicle_id = args.get("vehicle_id")

        if tool == "takeoff":
            refusal = self.check_altitude(args.get("target_altitude_m"))
            if refusal or not telemetry:
                return refusal
            return self.check_separation(
                vehicle_id, telemetry.get("latitude_deg"),
                telemetry.get("longitude_deg"),
                args.get("target_altitude_m"), fleet)

        if tool == "goto":
            refusal = (self.check_position(args.get("latitude_deg"),
                                           args.get("longitude_deg"))
                       or self.check_altitude(args.get("altitude_m")))
            return refusal or self.check_separation(
                vehicle_id, args.get("latitude_deg"),
                args.get("longitude_deg"), args.get("altitude_m"), fleet)

        # A vehicle already outside the fence must not be told to climb; that
        # is a worse version of where it already is.
        if tool in ("arm", "set_mode") and telemetry:
            return self.check_position(telemetry.get("latitude_deg"),
                                       telemetry.get("longitude_deg"))

        return None

    def describe(self):
        latitudes = [point[0] for point in self.fence]
        longitudes = [point[1] for point in self.fence]
        return (f"altitude {self.min_altitude_m:g}-{self.max_altitude_m:g} m; "
                f"operating area {min(latitudes):.5f}..{max(latitudes):.5f} lat, "
                f"{min(longitudes):.5f}..{max(longitudes):.5f} lon; "
                f"fleet separation {self.min_separation_m:g} m horizontal or "
                f"{self.min_vertical_m:g} m vertical")


DEFAULT_CONSTRAINTS = Constraints()
