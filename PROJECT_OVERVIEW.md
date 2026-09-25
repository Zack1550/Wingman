# Project Overview

**One line:** a natural-language mission layer for unmanned vehicles, where the
model proposes, code guarantees, and telemetry decides.

**Today** it is a simulator harness — two ArduPilot SITL copters on a laptop,
commanded from plain English through an approval gate. **The end state** is the
same software on a real aircraft that follows a hiker with no internet, with a
phone as the ground station.

**Two vehicles, one software stack.** A ground rover runs nearly all of the
same code and is the recommended *first* build: every hard problem left is
software, and on a rover you can debug it walking alongside at two miles an
hour for five hours a charge, instead of eighteen minutes at altitude.

Status of each piece is marked: **DONE** (built and verified), **PARTIAL**
(works, with a known gap), **DESIGNED** (written up, not built), **NEW** (not
started).

---

## 1. Where it is now

Built over one weekend against ArduPilot SITL. ~6,700 lines of Python,
149 unit tests passing in ~20 s with no Docker, 12 end-to-end missions graded
from traces rather than from the model's summary.

| Component | State | Note |
|---|---|---|
| `vehicle_gateway` — UDP/protobuf ⇄ MAVLink | DONE | Only component that speaks MAVLink. Idempotent on `operation_id`, per-vehicle `state_version`, link fault injection |
| `mcp_server` — 13 tools over stdio | DONE | The model's read/wait surface |
| `harness/planner` — plan as structured data | DONE | Validated against real tool schemas before anyone sees it |
| `harness/approval` — the human gate | DONE | Binds tool + canonical args + `state_version` + expiry + `op_id`; re-checked at dispatch |
| `harness/constraints` — geofence, ceiling, separation | DONE | Checked *before* the operator is asked. Both command paths call it |
| `harness/store` — SQLite command ledger | DONE | Record written before the datagram leaves |
| `harness/loop` — direct agent loop | DONE | Budget-guarded. No approval gate; for observation only |
| `evals` — three verdicts, offline re-grading | DONE | pass / correctly-declined / fail. 12 visible cases, 5 held out |
| `observability` — traces + telemetry → MCAP | DONE | Agent decisions and vehicle behaviour on one clock in Foxglove |
| Gateway dedup ledger durability | PARTIAL | In memory. A restart forgets every `op_id` |
| MAVLink reconnection | PARTIAL | Says `LINK DOWN` instead of serving stale state, but needs a manual restart |
| Fleet concurrency | PARTIAL | Missions are sequential; `wait_for_*` blocks |
| Operator interface | PARTIAL | CLI prompt. One operator, no authority model |
| Battery / range limits | NEW | No constraint exists; a model can fly a 0% aircraft |
| Semantic claim-grounding | PARTIAL | Structural checks only — can't tell a restated target from a claimed one |
| Document retrieval | NEW | — |

**Measured results (SITL, 12 missions):** Claude Sonnet 5 12/12 at $0.016/mission,
~9 s median model time. Local `granite4.1:3b` 3/12 — five failures produced no
usable plan at all. It drives tools fine and cannot plan.

---

## 2. End goals

### The end state: follow-me copter, no internet

A single copter that follows a hiker using onboard vision, commanded by voice
from a phone, with the whole stack running on the aircraft.

The load-bearing design decision, and the thing every requirement should be
checked against:

| | Fast loop | Slow loop |
|---|---|---|
| Rate | ~20 Hz | seconds |
| Where | entirely onboard | phone, over WiFi |
| Decides | where to be right now | what the mission is |
| Made of | vision + a controller + arithmetic | a model, a plan, an approval |
| If the link drops | keeps working | waits and resumes |

The model never steers. It requests a **state transition**, and a state machine
decides whether that transition is legal. One rule carries over unchanged from
the simulator: **the gateway stays the only writer to the flight controller.**

### The ground version: trail rover — recommended to build *first*

Same software, no hover. Not instead of the copter — **before** it.

Every genuinely hard problem left in this project is software: the local model
that scored 3/12, the state machine, plan-level approval, the follow
controller, the phone app. None of them care whether the vehicle has wheels or
props. On a rover you can debug all of it walking alongside the thing at two
miles an hour with a kill switch in your hand, for five hours per charge. A bug
costs a scraped chassis instead of a $350 camera in pieces and an afternoon up
a tree. Then build the copter once the autonomy works, with the only new
problem being the one it was always going to be — that flying is unforgiving.

~5.3 h runtime at ~80 W, 10.5 kg carry mass, 6.8 kg gear payload, 20.3 kg
all-up. Width 420 mm to clear maintained singletrack.

**What carries over untouched:** harness (planner, constraints, approval,
store), `vehicle_gateway` — ArduPilot Rover speaks the same MAVLink — camera
person-tracking, whisper, ollama, the phone PWA, geofence, separation, leash,
and seven of the ten flight states.

**What changes:**

| | |
|---|---|
| `mcp_server` | `takeoff` / `land` drop out; `wait_for_altitude` → `wait_for_arrival` |
| Follow controller | same structure, skid-steer kinematics instead of a velocity vector |
| Constraints | altitude cap **removed**; max tilt / max grade **new** — tip-over is the rover's stall |
| Sensors | upward rangefinder removed; OAK-D handles forward obstacles |
| States | `TAKEOFF` / `LANDING` gone. `STUCK` (wheels commanded, odometry says nothing moved — the state where the vehicle asks *you* for help) and `TIPPED` (past roll/pitch limit, motors off, only a human recovers it) are new |

**Every failure mode gets easier.** On the copter, `COMMS_LOST` needed a
timeout, a camera-lock check, a degradation ladder and a land-in-place
decision, because *doing nothing* is not available to an aircraft. A rover just
stops. The same collapse applies to low battery, to `STUCK`, and to a bad plan
from a 3B model. That is worth more than the five hours of runtime.

**Where it genuinely fails.** Works: fire roads, gravel, doubletrack,
maintained singletrack, packed dirt, modest roots and rocks, grades to ~30%. A
300 mm wheel climbs 50–75 mm unaided. Doesn't: stairs, water bars, step-ups
past ~80 mm, boulder fields, loose scree, blowdown, anything needing a hand.
Rooty singletrack will stop it repeatedly.

---

## 3. What has to be built

Grouped for requirements breakdown, roughly in dependency order. Groups A–D
are **vehicle-agnostic** — the same work whether the thing has wheels or props,
which is the whole argument for building the rover first. Group E is the only
part that differs by vehicle.

**A. Harden what exists (all doable in SITL, no hardware)**
1. Persist the gateway ledger with a bounded horizon; add MAVLink reconnection.
2. Flight state machine — transitions as data, `request_mode` as a guarded
   write tool, one test per transition row. DESIGNED; highest value before any
   hardware arrives.
3. Plan-level approval — bind to a hash of the whole plan, then re-run
   *constraints* (not approval) per leg. Needed for any queued mission.
4. Per-vehicle execution lanes for real concurrency.
5. Battery-and-range as a code constraint.

**B. New constraint shapes**
6. **Follow envelope** — min standoff and max leash measured from the moving
   operator, not from a map. The fleet-separation rule turned inside out.
7. Keep the static geofence as an outer bound, enforced both in
   `constraints.py` and in ArduPilot itself.
8. Distance leash + mission timeout for autonomous multi-leg flight.

**C. The follow behaviour — all new, and the bulk of the work**
9. Person detection + stereo depth on-camera; 3D tracking.
10. The 20 Hz controller: spatial coordinates → setpoint. Velocity vector for
    the copter, skid-steer kinematics for the rover — same structure, retuned.
11. Degradation ladder, walked one rung at a time, never skipped. Copter:
    camera lock → phone-GPS follow → position hold → land in place. Rover: the
    last rung is just *stop*, which is why every failure mode is cheaper on the
    ground.

**D. Ground station**
12. Phone PWA: voice capture, WebSocket telemetry, approval modal showing exact
    tool and args, hardcoded LAND button that bypasses the model, wake lock.
13. Offline speech-to-text and a local model on the aircraft.
14. WiFi AP on the aircraft (not the phone) plus a captive-portal responder, or
    Android will fight you.

**E. Vehicle-specific (pick per build)**
15. *Rover:* max tilt / max grade constraint — tip-over is the rover's stall.
    `STUCK` detection from wheel-encoder vs. GPS/visual-odometry disagreement,
    including the ask-the-operator-for-help interaction, which is genuinely new.
    `TIPPED` — motors off, human-only recovery.
16. *Copter:* upward rangefinder for branch strikes, the flight-PoE chain, the
    follow envelope's vertical component, and ArduPilot `FOLL_*` GPS fallback.

**F. Portfolio / role-facing**
17. LLM judge over traces for semantic claim-grounding, scored against the
    structural checks.
18. Retrieval as a tool, with a planted document proving retrieval supplies
    context and never authority.

---

## 4. Purchasing

Compute and sensing first — that group is shared by both vehicles and useful
on the bench before either exists. The rover list is second because it is the
recommended first build; the aircraft list is the same hardware plus flight.

### Compute & sensing — $872

| Item | Why | Cost | Status |
|---|---|---|---|
| Jetson Orin Nano Super | companion computer, WiFi included | $400 | **BOUGHT** |
| OAK-D Pro W PoE | stereo depth + on-camera inference ($679 list) | $350 | **BUYING** |
| SanDisk Optimus 5100 500 GB NVMe | root filesystem, Gen4 x4 2280 | $40 | NEXT |
| microSD 128 GB A2 | JetPack flash, spare boot | $20 | NEXT |
| TP-Link TL-POE160S | bench PoE, 802.3at gigabit | $22 | NEXT |
| TF-Luna | upward rangefinder — the branch sensor | $40 | NEXT |
| Galaxy S20 / S21 | ground station; dual-band L1+L5 GNSS | — | OWNED |

### Ground version (rover) — $1,830

Reuses the entire compute & sensing group above, so this is the only additional
spend. Note it needs its own Pixhawk and GPS if the copter is also being built;
if the rover comes first, that hardware moves to the aircraft later.

| Item | Cost |
|---|---|
| Chassis — 20×40 extrusion, deck plate, hardware | $180 |
| 4× 24 V planetary gearmotors with encoders | $380 |
| 4× 300 mm pneumatic wheels + hubs | $140 |
| 2× dual-channel motor drivers, 30 A+ | $160 |
| Pixhawk 6C + M10 GPS, standalone | $250 |
| 500 Wh Li-ion pack + BMS | $350 |
| Charger with Li-ion mode | $85 |
| Radiomaster Pocket + ELRS RX | $95 |
| DC PoE injector for the camera | $40 |
| Wiring, connectors, misc | $150 |

The rover also **skips the one part nobody sells finished**: on the ground you
use a boxed DC PoE injector instead of designing a boost-plus-PSE chain around
a 75 g budget.

### Aircraft — $1,560

| Item | Why | Cost | Status |
|---|---|---|---|
| Holybro PX4 Dev Kit X500 V2 | the aircraft in one box — 500 mm carbon frame, 4× 2216 motors, ESCs, 1045 props, PDB, Pixhawk 6C, M10 GPS, telemetry radio. Flash ArduPilot over it, ~30 min, no soldering | $700 | LATER |
| 3× 4S2P 21700 Li-ion, 130 Wh | ~18 min each. Molicel P45B-class | $390 | LATER |
| 1× 4S 5000 mAh LiPo | first hovers and tuning — don't risk a $130 pack on flight one | $45 | LATER |
| Charger (ISDT Q6 Pro / HOTA D6 Pro) | must have a **Li-ion mode**, not LiPo only | $85 | LATER |
| Radiomaster Pocket + ELRS RX | manual override — not optional | $95 | LATER |
| Remote ID broadcast module | FAA requirement above 250 g | $45 | LATER |
| DC 802.3af PSE module | flight PoE, 14.8 V pack → 48 V camera | $40 | LATER |
| microSD, straps, standoffs, Cat6 | FC logging, plus a payload plate to fabricate | $40 | LATER |
| Spares: props, arm, motor, ESC | highest expected-value line here — you will crash in the first ten flights, testing exactly the autonomy you built | $120 | LATER |

### Totals

Two build paths. They share the compute group, and if you build both you also
already own the charger and the radio — $180 of the aircraft list.

| Path | Compute | Vehicle | Total | Still to spend |
|---|---|---|---|---|
| **Rover only** (recommended first) | $872 | $1,830 | **$2,702** | $2,302 |
| **Copter only** | $872 | $1,560 | **$2,432** | $2,032 |
| **Both, rover first** | $872 | $3,390 − $180 shared | **$4,082** | $3,682 |

$400 already spent (Jetson). Next $122 — NVMe, microSD, bench PoE, TF-Luna —
is useful on the bench under either path and is worth buying now.

**The rover is not the cheap option.** It costs ~$270 more than the copter,
which is worth saying plainly. What the extra buys: five hours instead of
eighteen minutes, 15 lb of cargo instead of none, no FAA registration, no
Remote ID, no airspace restrictions, and a vehicle whose worst failure mode is
that it sits down.

---

## 5. Budgets that decide the design

Two numbers to hold onto, because they constrain requirements more than
anything in software.

### Copter

**Mass:** payload subtotal 689 g of Holybro's 1500 g allowance (46%). All-up
~2.41 kg with battery. Easiest mass to forget is the fabricated payload plate
and wiring — 130 g of the 689.

**Power:** avionics ~40 W; hover adds ~322 W. Usable 110 Wh ÷ 362 W ≈
**18 minutes**. Five-sixths of the power is just holding 2.4 kg in the air,
which is why tuning the 10 W camera load is pointless and why more battery is a
treadmill. Three packs (~1 h of airtime, $390) beats one bigger aircraft
(~34 min, ~$1,100) — you need a fresh pack at the next overlook, not a
continuous half hour.

**Regulatory:** 2.41 kg is well past 250 g — FAA registration and Remote ID
both apply. National Parks and designated Wilderness prohibit drones outright.
Check specific trails before the aircraft exists.

### Rover — the numbers invert

**Mass:** the binding constraint is not payload, it is **the carry**. Design
starts there and the battery comes out: 10.5 kg dry on one centre handle
(23 lb, short distances), 3 kg of pack into your backpack for a stream
crossing, 6.8 kg of gear, 20.3 kg all-up — a 34% payload fraction, which is
conservative for a ground vehicle. Width 420 mm is the number that decides
whether it fits your trails.

**Power:** ~34 W rolling + ~34 W on an 8% grade + 40 W avionics → ~74 W flat,
108 W climbing, 40 W stopped. At ~80 W mixed duty: 500 Wh × 0.85 ÷ 80 ≈
**5.3 hours.**

**The inversion that matters:** avionics were rounding error on the copter
(40 W against 322 W of hover) and are now *half* the budget, with the Jetson
alone at 15 W. That flips which optimisations pay — the
Pi-in-the-backpack idea that bought two minutes of flight would buy nearly an
hour of driving. It also means weight is almost free: doubling to 1000 Wh costs
3 kg and ~5 W of rolling resistance and takes you past ten hours. The only
thing stopping you is the carry.

**Regulatory:** none. No registration, no Remote ID, no airspace.

---

## 6. Open questions to resolve before committing

1. **Which local model clears the bar.** No internet means no Sonnet. Measured:
   `granite4.1:3b` 3/12 against Sonnet's 12/12. A 7–8B model at Q4 is the
   obvious next test, and the existing test suite answers it in 20 s on hardware
   already on the desk. **This is the one question that could invalidate the
   natural-language layer entirely — test it first, it costs nothing.**
2. **Will a rover actually clear your trails?** This is the one question the
   design cannot answer for you, and it decides the whole ground path. A 300 mm
   wheel climbs 50–75 mm unaided. Walk a trail you actually hike and look for
   step-ups taller than your fist — if there is one every fifty metres, the
   rover is the wrong vehicle and the copter was right all along. Tracks or a
   rocker-bogie would help and cost width, mass and the carry.
3. **Flight PoE.** The camera wants 48 V, the 4S pack gives 14.4 V. A DC-DC
   boost into a PSE module (Silvertel Ag5800-class) closes it — ~75 g for the
   whole chain. Nobody sells this as a finished module.
4. **Timeouts must be re-measured.** Every one is derived from a 3.3 s worst-case
   stall measured under WSL2/Docker. WiFi at 10 m has a different distribution.
   The method transfers; the numbers do not.
5. **Obstacle avoidance is out of scope and that is a real limit.** A geofence is
   a polygon, not a tree, and GPS degrades badly under canopy — exactly where
   position-hold and return-to-launch become unreliable.
6. **Approval as a standing authority.** Approving each setpoint is meaningless
   at 20 Hz. What gets approved is the behaviour and its bounds, revocable from
   the phone at any moment. Needs design.

---

## Reference

- `README.md` — what the harness does, what it cannot, and the evidence for both
- `notes/architecture.html` — every component, port and limit
- `notes/gateway-explained.html` — command lifecycle and the lost-ack path
- `notes/system.html` — follow-me stack: every box, wire and protocol
- `notes/deployment.html` — laptop vs. aircraft, and the gap between them
- `notes/flight-states.html` — ten states, three authorities, the FSM rules
- `notes/rover.html` — ground variant and what changes
- `notes/saturday.md` — ten things the first day's traces taught
