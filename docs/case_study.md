# Case Study: Modbus-to-Cloud Custom Data Layer

**Project type**: Industrial IoT / Embedded Systems / Data Infrastructure  
**Stack**: Python 3.10+, Modbus TCP, MQTT (QoS 1), SQLite, Custom Vanilla JS Dashboard  
**Target hardware**: Selec EM2M-1P (Energy) and Advantech ADAM-4017+ (Process I/O) over RS485
**Gateway**: Standard USR-DR404 or equivalent Serial-to-Ethernet module

*(For a visual breakdown of how the physical wiring, the hardware gateway, and this software layer interact, see the [Hardware & System Architecture](hardware_architecture.md) document).*

---

## The Industrial Use Case: High-Volume Pumping Station

This architecture was designed and proven for industrial environments where electrical and hydraulic data must be correlated in real time. 

In a municipal pumping station or industrial cooling loop, a **Selec EM2M** energy meter tracks the electrical power going *into* the massive pump motors, while an **Advantech ADAM-4017+** analog I/O module tracks the physical work coming *out* (flow rate, pipe pressure, reservoir tank level via 4-20mA sensors). 

If the Selec meter shows the motor pulling maximum current, but the Advantech module shows flow rate dropping, the pump is cavitating, blocked, or has a broken impeller. Normalizing these two completely different protocols into one JSON stream allows cloud infrastructure to calculate real-time mechanical efficiency and trigger predictive maintenance.

---

## The problem this software layer solves

Commercial Modbus RTU-to-TCP gateways (e.g. USR-DR404, ~₹2,400) do one thing exceptionally well: they convert RS-485 serial frames into Modbus TCP packets that a networked host can reach. They do this reliably, cheaply, and without any programming. This project does not compete with that hardware — it treats it as a purchased component.

What the gateway *cannot* do:

1. Tell you that register `0x0000` on the Selec EM2M-1P contains voltage in volts, stored as a two-register big-endian word-swapped IEEE-754 float (as stated in the EM2M-1P instruction manual).
2. Tell you that the two-register value `[0xE979, 0x42F6]` (in word-swap order) decodes to `123.456`, not `0.00000...` or a garbage float from a wrong byte-order assumption.
3. Continue collecting data if your network goes down for 20 minutes, then replay it in the original order when the connection is restored.
4. Normalize a Selec energy meter and a third-party flow sensor — completely different register maps, different byte orders, different scale factors — into the same JSON shape so one downstream consumer can handle both.

This project's software layer fills exactly those four gaps.

*(For a visual breakdown of how the physical wiring, the hardware gateway, and this software layer interact, see the [Hardware & System Architecture](hardware_architecture.md) document).*

---

## What was built

### 1. Device profile system (the core reusability mechanism)

Every device-specific detail — register addresses, byte order, scaling, units, alert thresholds — lives in a human-editable YAML file. The Python code is completely generic.

```yaml
# config/devices/selec_em2m.yaml  (addresses from EM2M-1P datasheet, Doc. OP639-V03)
device_name: "Selec EM2M-1P"
slave_id: 1
poll_interval_seconds: 5
register_type: "input"          # FC=0x04 per the datasheet

registers:
  - name: "voltage"
    address: 0x00               # Confirmed from manual: Voltage L-N at hex 0x00
    register_count: 2
    data_type: "float32"
    byte_order: "big_endian_word_swap"  # Manual: "Float (Swapped)"
    unit: "V"
    alert_range: [200.0, 250.0]
```

Adding support for a new device means writing one YAML file. No code changes. No redeployment of the Python layer.

### 2. Byte-order handling (the silent failure point)

Modbus has no mandated byte order for multi-register values. Different vendors use different conventions. A wrong byte-order assumption does not throw an error — it produces a plausible-looking wrong number. This is the most common source of silent bugs in Modbus integration work.

The codebase implements three explicitly separate decode functions, each with its own unit tests sourced from IEEE-754 arithmetic:

| Mode | Word order | Byte order within word | Typical vendors |
|---|---|---|---|
| `big_endian` | High word first | Big-endian | Standard |
| `little_endian` | Low word first | Little-endian | Some PLCs |
| `big_endian_word_swap` | Low word first | Big-endian | Selec, some Schneider |

The test suite includes cross-mode tests that verify a wrong byte order gives a *wrong* result — not just that the right mode gives the right result. This is the only reliable way to catch the silent failure.

### 3. Resilient buffering (no data loss during outages)

Every reading is written to a local SQLite database before any publish attempt. The publish step is a best-effort; the buffer is the source of truth.

```
Poll registers → Write to SQLite buffer → Attempt MQTT publish → Mark published only on PUBACK
```

If the network or MQTT broker goes down:
- Polling continues uninterrupted
- Readings accumulate in the buffer
- On reconnect, buffered records replay in original timestamp order — downstream consumers see the data with the timestamps of when it was actually measured, not when it eventually arrived

Duplicate publishes are prevented by only marking a record published after receiving an explicit MQTT PUBACK (QoS 1), not after calling the publish function.

### 4. Multi-vendor normalization

Two simulated devices with completely different register layouts produce identical JSON output structure:

```json
{"device_name": "Panel Meter — Bay 1", "field_name": "voltage", "value": 224.8, "unit": "V", "timestamp_utc": "..."}
{"device_name": "Pipeline Monitor — Line 1", "field_name": "pressure", "value": 3.42, "unit": "bar", "timestamp_utc": "..."}
```

One MQTT topic pattern (`gateway/+/+`), one web-socket subscription, one dashboard — regardless of how many different devices are on the RS-485 bus.

### 5. Fault isolation

One unreachable device does not stop polling for all others. Each device's poll cycle runs inside its own exception boundary. This is directly demonstrable: kill one simulator while the others keep running, and the output log shows the failure clearly isolated to the affected device while all others continue.

---

## What commercial gateways already do well

The USR-DR404 and similar gateway hardware do serial-to-network conversion reliably and cheaply. They handle RS-485 electrical signaling, Modbus RTU framing, CRC checking, and multi-device addressing on the bus — none of which needs to be reimplemented in software. This project does not attempt to replicate or improve that functionality.

The software layer described here is only valuable *above* that hardware — it depends on the gateway working correctly. If your deployment needs are purely "get Modbus data to Modbus TCP," the hardware alone may be sufficient.

---

## Technical stack choices (and why)

| Choice | Reason |
|---|---|
| SQLite (not PostgreSQL, Redis, etc.) | No separate service to run or deploy. An edge deployment running on a Raspberry Pi or embedded Linux box cannot assume a database server. SQLite requires only the Python standard library. |
| MQTT QoS 1 (not QoS 0) | QoS 0 (fire-and-forget) cannot confirm delivery, which breaks the replay logic. QoS 1 provides PUBACK so the buffer knows when a record is safely delivered. |
| pymodbus (not raw socket) | Handles Modbus TCP framing, exception response parsing, and connection management. Writing this from scratch would add risk without benefit. |
| YAML profiles (not a database schema) | Human-readable, version-controllable, deployable as plain files. A field technician can update a register address in a YAML file without a database tool. |

---

## What this is not

- Not a full SCADA system
- Not a replacement for purpose-built energy management software
- Not a solution for very high-frequency data (>10 Hz per device) without modifications to the polling architecture
- Not tested with all Modbus devices — only the Selec EM2M-1P register map has been verified against the official datasheet; other device profiles would need verification against each device's documentation

---

## Potential extensions (not built in this version)

- REST API endpoint to query the SQLite buffer directly (for integration with web frontends without MQTT)
- TLS/authentication for the MQTT broker (trivial with paho-mqtt; just needs broker config)
- Device health dashboard (last-seen timestamps, error rates per device)
- Support for writing holding registers (currently read-only)
