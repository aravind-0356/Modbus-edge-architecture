# AGENTS.md

This file tells any agentic coding tool (Claude Code, Antigravity, Cursor, etc.) how to
work in this repository. Read this before making changes. Also read `SKILLS.md` for
domain-specific technical patterns required by this project, and `WALKTHROUGH.md` for the
full phase-by-phase plan.

## Project summary

A custom data normalization and resilience layer built on top of a commercial Modbus
RTU-to-network gateway (e.g. USR-DR404). The gateway handles raw protocol conversion.
This codebase adds: device-aware register interpretation, local buffering with
replay-on-reconnect, threshold-based edge alerts, and multi-vendor normalization into one
consistent output format. Full rationale is in `WALKTHROUGH.md`, Phase 0.

## Setup commands

```bash
python -m venv .venv
source .venv/bin/activate
pip install pymodbus pyyaml paho-mqtt --break-system-packages   # if using system pip
pytest tests/
```

## Running the project

```bash
# Start simulated Modbus slaves (only needed if no real gateway/device attached)
python simulators/modbus_slave_sim.py --config config/devices/sim_device_a.yaml
python simulators/modbus_slave_sim.py --config config/devices/sim_device_b.yaml

# Run the polling/publishing loop
python src/main.py --config-dir config/devices/
```

## Code conventions

- Python 3.10+. Type hints required on all function signatures in `src/`.
- Device-specific behavior (register addresses, byte order, scaling, alert thresholds)
  lives ONLY in `config/devices/*.yaml`. Never hardcode a device name check in `src/`.
- Every function in `src/byte_order.py` must have a corresponding unit test with a known
  input/output pair sourced from real datasheet data or a verified real-device read —
  not fabricated numbers.
- Failures in one device's poll/publish cycle must not affect other devices. Isolate with
  try/except per device inside the main loop, and log the failure with device name and
  timestamp.
- Do not introduce a database server dependency. Local buffering uses SQLite
  (`src/buffer_db.py`) specifically because it requires no separate service to run or
  deploy — this is a portability requirement, not a preference.

## Testing instructions

- Run `pytest tests/` before considering any phase complete.
- `tests/test_byte_order.py` must pass for all supported byte-order modes before Phase 3
  begins — this is the highest-risk silent-failure point in the project (see
  `WALKTHROUGH.md` Phase 2).
- Manual verification steps described in `WALKTHROUGH.md` (Phase 1 and Phase 3 acceptance
  criteria) are not optional and are not replaced by unit tests — they check against real
  or simulated hardware behavior, not just code logic.

## Phase gating

This project is built in 7 phases, defined in full in `WALKTHROUGH.md`. Do not start a
phase's tasks until the previous phase's acceptance criteria (also defined there) are
met. If unclear whether a phase is complete, state what's missing and ask before
proceeding, rather than continuing on an assumption.

## Things to never do in this repo

- Never fabricate device datasheet values, register maps, or "expected" test outputs.
  If real data isn't available, use the simulator and label it clearly as simulated.
- Never commit `.env` files, device IPs, MQTT broker credentials, or any config under
  `config/local.yaml`. These are gitignored on purpose.
- Never present simulated demo data as real hardware data in `docs/case_study.md` or
  `docs/demo_script.md`.
- Never add a device-specific `if` branch in `src/` — that always belongs in a YAML
  profile instead.

## File map (for quick orientation)

| Path | Purpose |
|---|---|
| `WALKTHROUGH.md` | Full phase-by-phase build plan and acceptance criteria |
| `SKILLS.md` | Domain knowledge required (Modbus, byte order, buffering patterns) |
| `config/devices/*.yaml` | Per-device register maps — the reusability mechanism |
| `src/profile_loader.py` | Loads/validates device YAML profiles |
| `src/byte_order.py` | Register-to-value conversion, all supported endianness modes |
| `src/modbus_client.py` | Modbus TCP polling against the gateway |
| `src/buffer_db.py` | SQLite local buffering + replay-on-reconnect |
| `src/edge_rules.py` | Threshold/anomaly checks from profile config |
| `src/publisher_mqtt.py` | Publishes normalized records to MQTT |
| `src/main.py` | Orchestrates the full polling/buffer/publish loop |
| `simulators/modbus_slave_sim.py` | Fake Modbus devices for dev/testing without hardware |
| `dashboard/node-red-flow.json` | Exported Node-RED flow for the demo dashboard |
| `docs/demo_script.md` | Script for the portfolio demo video |
| `docs/case_study.md` | Portfolio writeup draft |
