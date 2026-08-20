# SKILLS.md

Domain knowledge required to implement this project correctly. Read this alongside
`AGENTS.md` (conventions) and `WALKTHROUGH.md` (phase plan). These are not optional
background reading — several are the exact points where this kind of integration project
silently produces wrong data if handled carelessly.

---

## Skill: Modbus RTU vs Modbus TCP

- **Modbus RTU** is the serial protocol spoken over RS-485 by the field devices
  (energy meters, VFDs, PLCs). It is binary, uses CRC-16 for error checking, and is
  master-poll only — a slave device never speaks unless polled by the master.
- **Modbus TCP** is what this project's code actually speaks. The commercial gateway
  (e.g. USR-DR404) sits between the RS-485 bus and the network, and performs the
  RTU-to-TCP conversion. This project's Modbus client code should never need to
  implement RTU framing or CRC handling directly — that's the gateway's job. If you find
  yourself implementing RTU frame parsing in `src/`, something is architecturally wrong;
  check the gateway's Modbus Gateway mode is actually configured.
- Function codes relevant here: **Read Holding Registers (0x03)** is the most common for
  reading meter/sensor data. Know that "coils" (single-bit, read/write) and "discrete
  inputs" (single-bit, read-only) are different register types from "holding registers"
  and "input registers" (both 16-bit) — a device profile must specify which type each
  named field is, don't assume all data is holding registers without checking the
  device's documentation.

## Skill: Byte order / endianness in multi-register values

This is the single most common source of silent, hard-to-detect bugs in Modbus
integration work. A wrong byte-order assumption does not throw an error — it produces a
plausible-looking but wrong number.

- A 32-bit value (e.g. an IEEE-754 float) spans two consecutive 16-bit Modbus registers.
  There are two independent orderings to account for:
  1. **Byte order within each 16-bit register** (which byte is most significant).
  2. **Word order between the two registers** (which register holds the high word).
- Different vendors combine these differently, and the Modbus spec itself does not
  mandate one standard, so this must be configured per device, never assumed globally.
- **How to determine the correct mode for a real device**: read a register whose
  real-world value you already know independently (e.g. read voltage while a meter's
  local display also shows the same value), then work out which byte-order mode
  reproduces that number. Do not guess and move on if the numbers look "close enough" —
  a wrong word-swap can still produce a plausible-looking wrong value.
- Implement each supported mode as an explicit, separately tested function
  (`src/byte_order.py`, see `AGENTS.md` testing instructions). Never implement a single
  "smart" auto-detecting function — auto-detection of byte order from ambiguous data is
  unreliable and hides bugs rather than surfacing them.

## Skill: Device profile design (the reusability mechanism)

- The entire "80% of future work already done" value proposition of this project depends
  on device-specific knowledge living in data (YAML), not code. Before writing any
  parsing logic, confirm the YAML schema (see `WALKTHROUGH.md` Phase 2) can express
  everything a new, unseen device profile would need — if a hypothetical new client
  device requires something the schema can't express, extend the schema, don't special-case
  it in code.
- Fail loudly on an invalid or incomplete profile at load time, not partway through a
  poll cycle. A bad profile should never produce a silently wrong reading — it should
  either be rejected at startup or clearly flagged per-field.

## Skill: Local buffering and replay-on-reconnect

- Why SQLite specifically: it is file-based, requires no separate server process, and
  Python's standard library includes it — this matters for a lightweight edge deployment
  where running a full database server isn't reasonable.
- Buffer-then-publish, never publish-then-buffer: every reading is written to local
  storage first, and only marked "published" after a confirmed successful publish. This
  ordering is what guarantees no data loss if the publish step fails mid-operation.
- Replay must preserve original order and original timestamps (the timestamp of the
  actual reading, not the timestamp of the eventual publish) — a dashboard or downstream
  system consuming this data needs to know when the value was actually measured, not
  when it happened to arrive.
- Avoid duplicate publishes on replay: only mark a record published after receiving
  explicit publish confirmation (e.g. MQTT PUBACK for QoS 1), not merely after calling
  the publish function.

## Skill: MQTT basics relevant to this project

- **QoS (Quality of Service) levels**: QoS 0 (fire and forget, no confirmation) is not
  sufficient here, since the replay logic depends on knowing whether a publish actually
  succeeded. Use QoS 1 (at-least-once delivery, with acknowledgment) at minimum.
- **Topic structure**: use a hierarchical topic per device/field
  (e.g. `gateway/<device_name>/<field_name>`) rather than one flat topic with everything
  crammed into the payload — this lets a dashboard or downstream consumer subscribe
  selectively, and is the conventional MQTT pattern for structured sensor data.
- **Public demo brokers** (e.g. HiveMQ's public broker) are fine for development and
  portfolio demos, but are not private or authenticated — never publish anything
  containing real client-identifying information to a public broker, only simulated or
  clearly anonymized demo data.

## Skill: Isolating per-device failures in a polling loop

- A multi-device polling system must not let one unreachable or misbehaving device (a
  timeout, a Modbus exception response, a malformed profile) stop polling for every other
  device. Wrap each device's poll-and-publish cycle in its own error boundary
  (try/except), log the specific failure with device name and timestamp, and continue the
  loop for the remaining devices.
- This is directly demoable: intentionally break one simulated device's connection while
  the others keep reporting, as evidence the isolation works — worth including as a
  secondary proof point alongside the Phase 4 buffering demo.

## Skill: Honest demo data practices

- Any reading, dashboard value, or case-study number that comes from a simulated device
  must be clearly labeled as simulated wherever it's shown to a prospective client
  (README, demo video, case study). Presenting simulated data as if it came from real
  hardware would misrepresent the portfolio piece — avoid this even informally in video
  narration or captions.
