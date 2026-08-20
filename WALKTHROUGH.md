# Modbus-to-Cloud Custom Data Layer — Project Walkthrough

## Project positioning (read this first, every phase must serve it)

We are NOT building a Modbus RTU-to-network gateway. Commercial hardware (e.g. USR-DR404,
~₹2,400) already does raw protocol conversion cheaply and reliably. That box is treated in this
project as a **purchased component**, not a competitor to be out-engineered.

The product being built is the **software layer above the gateway**: a device-aware,
resilient, multi-vendor data normalization system that a raw gateway cannot provide.

Three concrete differentiators to prove, in order:
1. **Device knowledge** — turns raw, unlabeled register values into named, scaled,
   real-world quantities (e.g. "230.4 V", not "raw register 2300").
2. **Resilience** — local buffering so no data is lost during a network outage, with
   automatic replay on reconnect.
3. **Multi-vendor normalization** — multiple different devices, each with a different
   register map/byte order/scaling, unified into one consistent output stream.

Every phase below should visibly produce evidence for one of these three points. If a task
doesn't serve one of them, deprioritize it.

---

## Repository structure to create

```
modbus-cloud-gateway/
├── AGENTS.md
├── SKILLS.md
├── README.md
├── config/
│   └── devices/
│       ├── selec_em2m.yaml
│       ├── sim_device_a.yaml
│       └── sim_device_b.yaml
├── src/
│   ├── profile_loader.py      # loads + validates device YAML profiles
│   ├── byte_order.py          # register -> real value conversion (endianness handling)
│   ├── modbus_client.py       # polls the gateway over Modbus TCP
│   ├── buffer_db.py           # SQLite local buffering + replay logic
│   ├── edge_rules.py          # threshold/anomaly checks from profile config
│   ├── publisher_mqtt.py      # publishes normalized records to MQTT
│   └── main.py                # orchestrates the polling loop
├── simulators/
│   └── modbus_slave_sim.py    # pymodbus-based fake Modbus devices for testing
├── dashboard/
│   └── index.html             # Custom vanilla JS dashboard for the demo
├── tests/
│   ├── test_byte_order.py
│   ├── test_profile_loader.py
│   └── test_buffer_db.py
└── docs/
    ├── demo_script.md         # exact steps for the 2-3 min demo video
    └── case_study.md          # portfolio writeup draft
```

---

## Phase 0 — Narrative lock-in (no code)

**Task**: Write `README.md` with the positioning statement above, verbatim intent, adapted
to plain prose. This file is the source of truth for "why" every later phase exists.

**Acceptance criteria**: README opens with a one-paragraph pitch that explicitly contrasts
what a commercial gateway does vs. what this project adds. No code until this exists.

---

## Phase 1 — Test environment setup

**Goal**: A real Modbus TCP endpoint to develop against (via the physical gateway) OR a
simulated one, before any parsing logic is written.

**Tasks**:
1. If real hardware is available: physically wire an RS-485 device (or the RS-485 side of
   a USB-RS485 adapter feeding a simulator) to the gateway's RS-485 terminals, and put the
   gateway on the local network (Ethernet preferred over WiFi for realism/reliability).
   Use the gateway vendor's own config tool (e.g. USR-TCP232-Test) to confirm a register
   can be read through the gateway. Do not write project code until this manual check passes.
2. If no hardware yet: build `simulators/modbus_slave_sim.py` using `pymodbus` in server
   mode. It must support running multiple slave instances on different ports/slave IDs,
   with independently configurable register maps, so Phase 6 (multi-device) doesn't
   require rewriting the simulator later.
3. Record connection details (IP, port, slave ID(s)) in a local `.env` or `config/local.yaml`
   (gitignored — do not commit device IPs or credentials).

**Acceptance criteria**: A raw register read succeeds against the real or simulated target,
independent of any project code, and is logged/screenshotted for reference.

---

## Phase 2 — Device profile / register map system

**Goal**: A reusable, human-editable schema describing how to interpret a specific device's
registers. This is the core reusability mechanism — new client device = new YAML file, not
new code.

**Schema** (`config/devices/*.yaml`):
```yaml
device_name: "Selec EM2M-1P"
slave_id: 1
poll_interval_seconds: 5
registers:
  - name: "voltage"
    address: 0
    register_count: 2
    data_type: "float32"
    byte_order: "big_endian_word_swap"   # must be one of the supported modes below
    scale: 1.0
    unit: "V"
    alert_range: [200, 250]              # used by edge_rules.py in Phase 4
  - name: "current"
    address: 2
    register_count: 2
    data_type: "float32"
    byte_order: "big_endian_word_swap"
    scale: 1.0
    unit: "A"
```

**Tasks**:
1. `src/profile_loader.py`: load and validate a YAML file against a minimal schema
   (required fields present, `byte_order` is a supported value, `data_type` is supported).
   Fail loudly (raise, don't silently default) on invalid profiles.
2. `src/byte_order.py`: implement conversion functions for at minimum these modes, since
   byte order is the single most common silent-failure point in Modbus integration work:
   - `big_endian` (standard word order, standard byte order within word)
   - `little_endian`
   - `big_endian_word_swap` (word order swapped, common on some vendor devices)
   Each function takes N raw 16-bit registers + declares which mode, returns the correct
   float/int value. Do not guess — every mode must be explicitly implemented and tested.
3. Write `tests/test_byte_order.py` with known input/output pairs for each mode
   (construct these from documented examples, do not fabricate expected values without
   a source — verify against the real device's datasheet or observed behavior from Phase 1).

**Acceptance criteria**: Given a raw register set and a declared byte_order mode, the
correct real-world float is produced, verified by unit test, not just manual inspection.

---

## Phase 3 — Data ingestion middleware

**Goal**: Poll the gateway (Modbus TCP client, since the gateway already did the RTU→TCP
conversion) on a schedule, apply a device profile, output clean labeled records.

**Tasks**:
1. `src/modbus_client.py`: wrap `pymodbus`'s TCP client. Function signature roughly:
   `poll_device(ip, port, profile) -> list[Reading]`, where `Reading` is a small dataclass:
   `{device_name, timestamp_utc, field_name, value, unit}`.
2. `src/main.py`: load all profiles in `config/devices/`, run a polling loop (respecting
   each profile's own `poll_interval_seconds`), print/log clean JSON output.
3. Handle Modbus exception responses and connection failures explicitly — do not let an
   unhandled exception crash the polling loop for one device and take down polling for
   all other devices. Each device's poll cycle should be isolated (try/except per device).

**Acceptance criteria**: Running `python src/main.py` against Phase 1's target produces a
continuous stream of labeled JSON records with correct real-world values (cross-checked
manually against the raw register read from Phase 1).

---

## Phase 4 — Edge logic and local buffering

**Goal**: Prove resilience — the second differentiator.

**Tasks**:
1. `src/buffer_db.py`: SQLite schema, minimum columns:
   `id, device_name, timestamp_utc, field_name, value, unit, published (bool)`.
   Every reading from Phase 3 is written here FIRST, before any publish attempt.
2. Replay logic: on each publish cycle, query `WHERE published = 0 ORDER BY timestamp_utc`,
   attempt publish in order, mark `published = 1` only on confirmed publish success.
3. `src/edge_rules.py`: read `alert_range` (or similar) from the device profile, flag
   readings outside range. Flagged readings get a separate, immediate publish path
   (don't wait for the normal batch interval) — this is a concrete "edge intelligence"
   demo point.
4. Build a deliberate test: kill network connectivity (or point the publisher at an
   invalid broker temporarily) for a few minutes while polling continues, then restore
   it, and confirm buffered records flow in correctly, in order, with no duplicates.

**Acceptance criteria**: A recorded test showing readings continuing to be captured during
a simulated outage, and successfully replayed on reconnect, with no data loss and no
duplicate publishes.

---

## Phase 5 — Cloud / dashboard integration

**Goal**: A visual, demoable destination for the data.

**Tasks**:
1. `src/publisher_mqtt.py`: publish each unpublished record from `buffer_db.py` as JSON
   to an MQTT topic (e.g. `gateway/<device_name>/<field_name>`), using a free-tier broker
   (HiveMQ public broker for demo purposes — do not use it for anything beyond demo/dev,
   it is not private).
2. Custom web dashboard (`dashboard/index.html`): Subscribe to the MQTT broker using WebSockets
   (`mqtt.js`) and render the data dynamically using Chart.js.

**Acceptance criteria**: Live values appear on the HTML dashboard within one poll
interval of being published, labeled with device name and unit (not raw register numbers).

---

## Phase 6 — Multi-device aggregation demo

**Goal**: The headline proof point. One device proves normalization works; two-plus
devices with genuinely different register layouts prove it generalizes.

**Tasks**:
1. Configure `sim_device_a.yaml` and `sim_device_b.yaml` with deliberately different
   `byte_order`, register addresses, and scale factors, to mimic two different real vendors.
2. Run both simulated slaves concurrently via `simulators/modbus_slave_sim.py`.
3. Run `src/main.py` against both simultaneously, confirm the output stream/dashboard
   shows both devices' data in the same normalized format (same field naming convention,
   same units, same JSON shape) despite different underlying register layouts.

**Acceptance criteria**: Dashboard shows 2+ devices side by side, values are correct for
each despite different profiles, and this is captured on video for Phase 7.

---

## Phase 7 — Packaging and publishing

**Tasks**:
1. `docs/demo_script.md`: exact narration + on-screen sequence for a 2-3 minute demo video:
   (a) show raw gateway output (unlabeled registers) vs. normalized output side by side,
   (b) show live dashboard with 2+ devices,
   (c) trigger a connectivity drop, show buffering continuing,
   (d) restore connectivity, show buffered data replaying with correct timestamps.
2. `docs/case_study.md`: portfolio writeup draft. Must explicitly and fairly acknowledge
   what commercial gateways already do well (do not disparage the hardware), then state
   the specific gap this project fills, using the three differentiators from the top of
   this document.
3. Clean up `README.md` with setup instructions (how to run simulators, how to add a new
   device profile, how to run the dashboard) so the repo is usable by someone else,
   not just self-documenting for the author.

**Acceptance criteria**: A fresh clone of the repo, following only the README, can run the
full simulated demo end-to-end without undocumented manual steps.

---

## Explicit constraints for the agent (apply throughout all phases)

- Do not fabricate or guess Modbus register values, byte-order behavior, or device
  datasheet numbers. If a real device isn't available yet, use the simulator and label
  all demo data as simulated in the README and case study — do not present simulated
  numbers as if they came from real hardware.
- Do not silently swallow exceptions in the polling or publish path. Log and isolate
  failures per-device; never let one device's failure stop polling for others.
- Do not skip the manual verification step at the end of Phase 1 and Phase 3 — these are
  the points where a bug (e.g. wrong byte order) would otherwise go undetected until much
  later and be harder to trace.
- Keep device-specific logic entirely inside YAML profiles, not hardcoded in `src/`. If you
  find yourself writing an `if device_name == "..."` branch in application code, stop —
  that logic belongs in the profile schema instead.
- Ask for explicit confirmation before moving to the next phase if the current phase's
  acceptance criteria were not clearly met.
