# Wingman Harness

A natural-language mission orchestrator for simulated multirotor aircraft, built
against ArduPilot SITL. 

End goal: A single natural-language fleet orchestrator
capable of running single craft or a fleet.

Scoping limits: As a personal project the harness will only manage
a "fleet" consisting of a rover and multirotor aircraft.

You describe a mission in English. A model turns it into a **plan** — ordered
steps with exact arguments, as data rather than prose. A human approves each
risky command. A separate executor runs only what was approved, over a
**UDP/protobuf gateway** whose link can be made to drop, duplicate, reorder and
delay. Nothing claims success that telemetry did not show.

```
  you ──► planner ──► a plan, as data       the model proposes.
                            │               It never invokes anything.
                            ▼
                       executor.py
                            │
       1. constraints ──────┤   geofence, ceiling, fleet separation.
                            │   A violation is blocked here, and the
                            │   operator is never asked.
       2. approval ─────────┤   a human, on one exact command — only
                            │   for anything that can move an aircraft.
       3. re-check ─────────┤   same args? same state_version? not
                            │   expired? Checked at the moment of send.
       4. dispatch ─────────┤
                            │
         ┌──────────────────┴───────────────────┐
         │ writes                               │ reads and waits
         │ the executor owns the op_id,         ▼
         │ so these bypass MCP        mcp_server/server.py · 13 tools
         │                                      │
         ▼                                      ▼
                 vehicle_gateway/client.py
                            │  UDP/protobuf
                            │  14550      commands + acks
                            │  14551/2/3  telemetry @ 5 Hz
                            ▼
                        gateway.py
            dedup by op_id · state_version · preconditions
                    · link fault injection
                            │  MAVLink  tcp/5760
                            ▼
                    mavlink-router ──► copter_1 (sysid 1)
                                   └─► copter_2 (sysid 2)
```

There is a second, simpler route: `harness/run.py` puts a model in a loop where
it calls the MCP tools directly, guarded by step, tool-call and deadline
budgets. That path has no approval gate — it is for watching a model work, not
for flying anything you'd be sad to lose.

---

## How it works

Give the system a mission in plain English. A model turns that into a
**plan** — an ordered list of steps, each naming an exact tool with exact
arguments, emitted as structured data rather than prose. That distinction is the
hinge the whole design turns on: a plan in prose can be read multiple ways, and a
permission granted against it means imprecise execution. A plan as data has one
reading, so a human can approve one specific command and the system can later
prove that only that command ran. The planner is given the aircraft's current
state before it plans. Every generated step is validated against the tool's schema
before an operator is asked to approve anything.

Underneath sits a **UDP/protobuf gateway** that is the only component speaking
MAVLink. Everything above it deals in `VehicleCommand` and `VehicleTelemetry`
messages; everything below it deals in scaled integers, type masks and `MAV_CMD`
numbers. The gateway keeps a ledger keyed on a client-generated `operation_id`,
which makes retrying free — three identical datagrams move the aircraft once and
advance its `state_version` once. That version increments on every accepted
command and rides along in telemetry, so any caller can tell whether the world
has moved since it last looked. The gateway also enforces the preconditions that
must hold regardless of how a command arrived: armed, in GUIDED, not already
airborne.

The gateway's link can be made to **drop, duplicate, reorder and delay**, because
a harness that has only run over loopback has never been tested. When an
acknowledgement goes missing, the correct behaviour is neither to retry nor to
report failure: the outcome is genuinely *unknown*, and unknown is a state the
system reports rather than a coin it flips. The client asks the gateway what it
recorded for that specific `operation_id` and gets the original acknowledgement
back verbatim — a lookup by key, not an inference from a state change, which
matters the moment two things can command one aircraft. In a recorded mission
the aircraft was already descending 1.6 seconds after the command while the
harness did not learn its command had landed for six more seconds. That window
is not an error condition; it is the normal state of a distributed system, and
every safety property here is designed to survive it.

There are **two routes from a model to an aircraft**, and they differ in what
stands between intent and action. The agent loop lets a model call tools
directly, guarded by step, tool-call and deadline budgets, with argument
validation and unknown-tool rejection returning structured errors the model can
recover from rather than exceptions that kill the run. The plan path adds a
human: constraints are checked *before* the operator is asked — a prompt for
something the system will refuse anyway teaches people to click through — and
approval binds to the tool, the canonical arguments, the telemetry version it was
proposed against, an expiry and the `operation_id` it will use. That binding is
re-checked immediately before dispatch, because a person takes seconds to read a
prompt and the aircraft does not pause for them. Geofence, altitude ceiling and
fleet separation live in one module both paths call, since they do not share a
code path to the vehicle.

Everything is **written down before it is needed**. The ledger record exists
before the datagram leaves, so a crash mid-flight is recoverable and a restart
reconciles outstanding commands instead of resending them. Traces capture one
JSONL line per event — plan, approval, dispatch, outcome, model latency,
reasoning summary — and a separate recorder captures the 5 Hz telemetry
broadcast, which merge into a single MCAP file so agent decisions and aircraft
behaviour sit on one scrubbable clock in Foxglove. Correctness is judged from
that evidence rather than from the model's closing paragraph: 149 layer-one tests
run in 20 seconds against an in-process fake gateway with no Docker, and twelve
end-to-end missions grade to three verdicts — pass, correctly declined, or fail —
because a refusal only counts if the reason it gave was true.

---

## Quick start

Requires Docker, Python 3.12 and (optionally) Ollama.

```bash
# 1. the simulator: two copters behind mavlink-router on tcp/5760
cd ardupilot_sitl_docker/stacks/n_copters
docker compose -f docker-compose-2.yml up -d
cd -

# 2. the gateway (wait ~60 s first for the EKF to settle)
python vehicle_gateway/gateway.py

# 3. layer-one tests — no Docker needed, they run against an in-process fake
pytest                                    # 149 tests, ~20 s

# 4. a mission, with an approval prompt per risky command
export ANTHROPIC_API_KEY=...
python -m harness.mission --provider anthropic --model claude-sonnet-5 \
    "Take off to 20 metres with copter_1 and hover there."
```

Useful variants:

```bash
python -m harness.mission --plan-only "..."     # see the plan, fly nothing
python -m harness.mission --refuse-all "..."    # prove a refused plan moves nothing
python vehicle_gateway/gateway.py --drop-first-ack   # swallow the first ack per op
python -m evals.runner --provider anthropic --model claude-sonnet-5
python -m evals.report --markdown
```

---

## What it does, against what the role describes

| Described publicly | This build | Honest gap |
|---|---|---|
| Reads platform docs and tool definitions, emits vehicle-native commands | MCP tool server → UDP/protobuf gateway → pymavlink → SITL | One vehicle type, one API I defined myself. No document retrieval yet |
| Builds a mission plan, submits for commander approval | Planner emits a schema-conformant plan; approval binds to canonical arguments, telemetry `state_version` and an expiry | CLI approval, not a C2 UI |
| Tasks each asset, monitors, re-plans in real time | Executor with a SQLite ledger, telemetry readback, bounded retries | Seconds-scale polling; missions are sequential, not concurrent |
| Enforces fleet constraints and operational authorities | Geofence, altitude cap, fleet separation, arm-requires-approval — enforced in code both paths reach | No authority model; one operator, one role |
| Runs at the edge under degraded comms | Link can drop, duplicate, reorder and delay; the harness reconciles a lost ack rather than guessing. Same harness runs on Ollama offline | A laptop, not hardened compute |
| Nightly retrain on mission logs (Foxglove / MCAP) | Agent steps and telemetry to MCAP, opened in Foxglove | Data preparation only — no fine-tuning was performed |

---

## Results

Twelve natural-language missions, end to end against SITL. Every trial resets
the simulator: the simulated battery drains by flying and persists across runs,
so without a reset the twelfth mission is flown by a different aircraft than the
first.

| Model | Cases | Acceptable | Fail | Median model time | Median wall time | Cost | Cost/case |
|---|---|---|---|---|---|---|---|
| `claude-sonnet-5` | 12 | **12/12 (100%)** | 0 | 10 s | 131 s | $0.19 | $0.016 |
| `granite4.1:3b` (local) | 12 | **3/12 (25%)** | 9 | 68 s | 179 s | $0.00 | $0.000 |

Median wall time includes the ~90 s reset, which is a property of this laptop
rather than of either model.

**Where the local model fails is more interesting than the rate.** Five of
granite's nine failures are `no_plan` — it could not turn the mission into an
ordered structure at all. The other four produced a plan that broke during
execution. It reads state and reports it correctly (`read_only`: 1/1); it cannot
reliably plan (`takeoff_hover`, `state_aware`, `land_recover`, `navigate`: 0/2,
0/2, 0/2, 0/1). Driving tools one at a time is a different capability from
emitting a whole plan, and only the second one broke.

Five further cases are held out in `evals/reserved/` and were not consulted
while tuning.

### Three verdicts, not two

A refusal is only correct if the reason it gives is true. Grading reads the
trace, the ledger and final telemetry — never the model's closing paragraph,
except where the entire point is to check it.

- **pass** — the mission was carried out and the assertions hold
- **declined** — the model refused, and the grounds it cited were true of the vehicle
- **fail** — anything else, including a refusal with no grounds

Outcome assertions alone would score a correct refusal as a failure. They would
also pass a model that reached the right altitude having claimed to observe it
from a reading of 9.93 m, which is why `observed_arrival` checks that *some tool
actually saw the aircraft near the target*, whatever the prose says.

---

## The lost acknowledgement

The property the whole gateway exists to drill. Run the gateway with
`--drop-first-ack` and the first reply for each `operation_id` is swallowed on
the way out. The vehicle still receives the command and acts on it; the client
hears nothing.

From `captures/mission.mcap`, agent decisions and vehicle telemetry on one clock:

```
15:21:03.0  HARNESS   approval granted, goto submitted        op-e1c3da131f46
15:21:03.0  GATEWAY   -> ACCEPTED: setpoint sent
15:21:03.0  GATEWAY   LINK dropped first ack (vehicle acted anyway)
15:21:04.6  VEHICLE   altitude 19.1 m, state_version 1        <- already moving
15:21:05.6  VEHICLE   altitude 18.2 m
15:21:09.0  GATEWAY   status query op-e1c3da131f46 -> known
15:21:09.0  HARNESS   command_outcome: applied, reconciled=true
```

For six seconds the aircraft was descending and the harness did not know its
command had arrived. Nothing was wrong with the aircraft, the command, or the
MAVLink link — the dropped datagram was the gateway's acknowledgement to the
*harness*, on `udp/14550`.

The recovery is a **lookup, not an inference**. The harness asks
`CommandStatusQuery(op_id)` and receives the acknowledgement the gateway
recorded, verbatim. Deducing success from `state_version` changing would be
ambiguous the moment two things can command one vehicle; a key lookup gives
attribution. `is_known: false` is equally informative in the other direction —
the operation never arrived, so re-sending is safe.

Three properties follow, and each is enforced rather than hoped for:

- **The `operation_id` is client-generated and stable across retries**, and the
  gateway deduplicates on it. Three identical datagrams move the vehicle once
  and advance `state_version` once.
- **The ledger record is written before the datagram leaves.** A record that
  only appears after a successful send is useless in exactly the case it exists
  for.
- **`unknown` is a first-class outcome.** Silence is not failure. A write that
  cannot be resolved is reported as unknown and the model is told not to guess.

Open the log yourself: [app.foxglove.dev](https://app.foxglove.dev) → Open local
file → `captures/mission.mcap`. Add a Plot panel on
`/copter_1/state.altitude_m` and a Log panel on `/agent/log`, then drag the
playhead to where the altitude starts falling.

---

## Layout

| Path | Lines | |
|---|---|---|
| `vehicle_gateway/gateway.py` | 884 | UDP/protobuf ⇄ MAVLink. Dedup ledger, per-vehicle `state_version`, link fault injection, telemetry fan-out |
| `vehicle_gateway/client.py` | 356 | The coping half: `operation_id` generation, send → timeout → status-query reconciliation, telemetry freshness |
| `vehicle_gateway/protos/vehicle.proto` | 109 | The wire contract |
| `mcp_server/server.py` | 494 | 13 MCP tools over stdio. Docstrings are prompts; `op_id` is not model-facing |
| `harness/planner.py` | 293 | Model proposes a plan as data, validated against real tool schemas |
| `harness/approval.py` | — | The gate. Nothing a model writes reaches it |
| `harness/executor.py` | 363 | Runs approved steps; re-checks the binding immediately before dispatch |
| `harness/store.py` | 320 | SQLite: `proposed → awaiting_approval → submitted → applied \| failed \| unknown` |
| `harness/constraints.py` | 219 | Geofence, ceiling, fleet separation — checked before the operator is asked |
| `harness/loop.py` | 173 | Provider-agnostic agent loop with step, tool-call and deadline guards |
| `evals/` | — | Two layers, three verdicts, offline re-grading |
| `observability/` | — | Telemetry capture and MCAP builder |

**149 layer-one tests** run in ~20 s with no Docker, against an in-process fake
gateway. Eight more run against live SITL under `pytest -m live`.

---

## Design decisions worth defending

**Constraints are checked before the operator is asked.** A prompt for something
the system will refuse anyway teaches people to click through prompts. There is
a test named for it.

**Approval binds to the exact command.** Tool, canonical arguments, the
telemetry `state_version` it was proposed against, an expiry, and the
`operation_id` it will use. Any change invalidates it, and the binding is
re-checked at dispatch — a human takes seconds to read a prompt, and the
aircraft does not pause for them.

**A rejection ends the run.** Not skip-and-continue, not ask-differently.

**Tolerances live in tools, not in arguments.** Offered a `min`/`max` band for a
45 m target, a model chose 40–50 and reported success at 40.6 m while still
climbing. `ALTITUDE_TOLERANCE_M` is a module constant; the model passes the
target it was given.

**Timeouts are derived from a measurement.** This stack stalls ~3.3 s every
~34 s — measured with a bare pymavlink listener and no gateway running, so it is
WSL2/Docker/SITL and not this code. Every timeout sits above that floor;
`TRANSPORT_WORST_GAP_S` records it. Re-measure on other hardware.

**Two model calls, not nine.** A mission costs $0.016 on the plan-then-execute
path against roughly $0.12 on the agent loop, because the loop resends the whole
conversation every step while the plan path calls the model twice. An 8×
difference from an architecture choice rather than a model choice.

---

## Known gaps

- **The gateway's dedup ledger is in memory.** Restart it and every
  `operation_id` is forgotten, so a retry across that boundary will re-execute.
  A real vehicle needs this in non-volatile storage with a bounded horizon.
- **The gateway cannot reconnect a dead MAVLink link.** It stops broadcasting
  and says `LINK DOWN` rather than serving stale state, which is the important
  half, but it still needs a restart.
- **Missions are sequential.** `wait_for_position` blocks, so "Bravo holds until
  Alpha arrives" is expressed as ordering, not concurrency. Real concurrency
  needs per-vehicle lanes in the executor.
- **Approved writes bypass the MCP tool layer** so the executor can own the
  `operation_id`. Constraints therefore live in a module both paths call;
  anything added to a tool body alone would not protect the executor path.
- **No battery or range constraint.** A model that flies a 0% aircraft is not
  making a mistake this system ever told it not to make. If battery matters it
  belongs in `constraints.py`.
- **Claim-grounding is structural, not semantic.** `cites_observed_value` cannot
  distinguish a restated target from a claimed achievement; that wants an LLM
  judge over the trace.
- **No document retrieval** and **no fine-tuning** — see the gaps table above.
- One vehicle type, one API I defined myself, and a laptop.

---

## What I would do next

1. Persist the gateway ledger and add reconnection, so a gateway restart is
   survivable rather than merely loud.
2. Per-vehicle execution lanes, so a fleet mission is concurrent rather than
   ordered.
3. A battery-and-range constraint, so the aircraft's endurance is enforced in
   code instead of left to the model's judgement.
4. An LLM judge over traces for semantic claim-grounding, scored against the
   structural checks to see where they disagree.
5. Retrieval as a tool, with a planted document proving that retrieval supplies
   context and never authority.

---

## Attribution

- Simulator: [ArduPilot SITL](https://ardupilot.org/dev/docs/sitl-simulator-software-in-the-loop.html),
  run via [`ardupilot_sitl_docker`](https://github.com/arthurrichards77/ardupilot_sitl_docker)
  (vendored as a separate clone, not committed here) with
  [mavlink-router](https://github.com/mavlink-router/mavlink-router).
- Protocol handling: [pymavlink](https://github.com/ArduPilot/pymavlink).
- Tooling: [Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk),
  [Foxglove SDK](https://docs.foxglove.dev/) and [MCAP](https://mcap.dev/).
- **Built with AI assistance.** Claude was used throughout as a pair programmer:
  writing code, running experiments against SITL, and finding bugs — several of
  its own. Architecture, design decisions and the direction of every block were
  mine; the reasoning behind each is in the commit messages, which are written
  to be read.

Models: `claude-sonnet-5`, `claude-opus-5`, `granite4.1:3b` (local, via Ollama).


#### Lessons learned
Nearly every serious bug turned out to be the same mistake in different clothes: something reporting an outcome it had never verified. The gateway kept broadcasting an aircraft's last-known state after its own uplink died. The executor stamped a read as success without inspecting what came back, so a mission whose confirming step returned gateway_unavailable reported completed. The eval reset slept eight seconds and hoped the simulator was ready; stop_gateway sent a signal and assumed the port was free. I spent two days criticising models for claiming success telemetry hadn't shown them, and found the identical error four times in my own code — which is the honest version of "prompts ask, code guarantees." The corollary was that the highest-leverage change all weekend wasn't a prompt at all: rewriting one rejection from "vehicle answered FAILED to arm" to "cannot arm while in RTL; set_mode to GUIDED first" turned a model that retried blindly and gave up into one that recovered on the next step. Same model, same prompt — better words from the machine.

#### Interesting things.
A link that died every 33 seconds turned out not to be my code: the simulator stack stalls ~3.3 s every ~34 s, reproducible with a bare pymavlink listener and no gateway running, which meant every timeout I'd chosen as a round number sat below the transport's actual floor. Given a min/max band to pick for a 45 m target, a model chose 40–50 and declared success at 40.6 m while still climbing — so tolerance moved out of the arguments and into the tool as a constant. Two frontier models given identical evidence disagreed about whether to fly a 0%-battery aircraft: one refused, cross-checked the second vehicle to rule out a broken field, and offered alternatives; the other flew it. And the local 3B model's failures weren't where I expected — it drove tools competently one at a time and failed at planning, five of nine failures producing no usable plan at all.