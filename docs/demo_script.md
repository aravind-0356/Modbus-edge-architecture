# Demo Script — Modbus-to-Cloud Custom Data Layer

**Format**: 2–3 minute screen recording with narration  
**Audience**: Upwork/Fiverr clients evaluating embedded systems freelance work  
**Hardware Context**: Selec EM2M-1P (Electrical) and Advantech ADAM-4017+ (Process). The demo uses data generators to simulate these devices so evaluators can run the repo locally, but the register maps and architecture are proven in production.

---

## Pre-recording checklist

- [ ] Start simulator A: `python simulators/modbus_slave_sim.py --config config/devices/sim_device_a.yaml`
- [ ] Start simulator B: `python simulators/modbus_slave_sim.py --config config/devices/sim_device_b.yaml`
- [ ] Confirm both ports are listening: `netstat -an | findstr "5021 5022"`
- [ ] Open `dashboard/index.html` in a web browser
- [ ] Browser showing the Ledger dashboard on one half of screen
- [ ] Terminal window visible on other half
- [ ] Internet connected (for HiveMQ public broker)

---

## Scene 1 — The problem (0:00–0:25)

**On screen**: A terminal running a raw Modbus read script (not part of this project — use `python -c "from pymodbus.client import ModbusTcpClient; c = ModbusTcpClient('127.0.0.1', port=5021); c.connect(); r = c.read_holding_registers(0, 4, slave=2); print(r.registers); c.close()"`)

**Narration**:
> "Here's what a raw Modbus gateway gives you. The gateway connects your RS-485 field devices to the network — and that's where its job ends. You get register numbers. No units, no labels, no idea whether 0x4366 means 230 volts or something's on fire."

**Show**: Raw register output like `[0x4366, 0x0000, 0x4208, 0xCCCD]` in terminal.

---

## Scene 2 — Normalized output (0:25–0:55)

**On screen**: Start `python src/main.py --config-dir config/devices/ --no-publish` (log-only mode to show JSON before MQTT)

**Narration**:
> "Now we add the software layer. The same registers, run through a device profile that knows what the registers mean, gives you this instead."

**Show**: JSON lines appearing:
```json
{"device_name": "Panel Meter — Bay 1", "timestamp_utc": "2024-01-15T10:30:00+00:00", "field_name": "voltage", "value": 224.8, "unit": "V", "alert": false}
{"device_name": "Pipeline Monitor — Line 1", "timestamp_utc": "2024-01-15T10:30:00+00:00", "field_name": "pressure", "value": 3.42, "unit": "bar", "alert": false}
```

**Narration**:
> "Named fields. Real units. Timestamps. Two completely different devices — different byte orders, different register maps — unified into the same JSON shape. Adding a new device profile means writing one YAML file. No code changes."

---

## Scene 3 — Live dashboard with 2 devices (0:55–1:30)

**On screen**: Stop the `--no-publish` run. Start `python src/main.py --config-dir config/devices/` (with MQTT enabled). Switch focus to the Ledger dashboard in browser.

**Narration**:
> "With MQTT publishing enabled, the same data flows to a real-time dashboard. In a pumping station, this lets us correlate domains. Device A is a Selec EM2M meter tracking electrical power going into the pump motor. Device B is an Advantech analog module tracking the physical hydraulic work coming out—flow, pressure, tank level. If current spikes but flow drops, the pump is cavitating. By normalizing both domains, we can calculate real-time mechanical efficiency in the cloud."

**Highlight**: Show both device cards updating simultaneously. Point out the device labels and the amber polling sweep-line animating on the cards.

---

## Scene 4 — Buffering during outage (1:30–2:15)

**On screen**: With `src/main.py` still running and data flowing to dashboard:

**Step 1** — Point the publisher at a bad broker to simulate outage:
```
# Kill and restart with bad broker
python src/main.py --config-dir config/devices/ --mqtt-host 127.0.0.1 --mqtt-port 9999
```

**Narration**:
> "Now I'm going to simulate a network outage — the MQTT broker is unreachable. Watch the sync status indicator in the top right."

**Show**: Dashboard charts stop updating. The sync pill in the top right switches from green `✓ synced` to amber `⬡ queued N` and the number counts up. Terminal shows polling continuing.

**Narration**:
> "Polling never stopped. Every reading is written to a local SQLite buffer first — the network outage doesn't cause any data loss. The UI lets operators know data is buffering, not broken."

---

## Scene 5 — Replay on reconnect (2:15–2:45)

**On screen**: Restore the correct broker:
```
python src/main.py --config-dir config/devices/
```

**Narration**:
> "Reconnect to the broker — and watch the buffer drain. The sync indicator switches to replaying, and the buffered readings flow in."

**Show**: Dashboard sync pill changes to blue `↺ replaying N` counting down rapidly, then back to green `✓ synced`. Dashboard charts visibly catch up with a burst of updates.

**Narration**:
> "Original timestamps are preserved — the dashboard gets the readings in the order they were actually measured, not the order they happened to arrive. No data loss. No duplicates."

---

## Scene 6 — Wrap-up (2:45–3:00)

**On screen**: Split view — Ledger dashboard on left, terminal showing clean JSON on right.

**Narration**:
> "The gateway handles serial protocol conversion. This layer handles everything above it: device-aware interpretation, resilient buffering, multi-vendor normalization. All the device-specific knowledge lives in YAML profiles — adding a new device is a config change, not a code change."

**End card**: GitHub repo link / Upwork profile.

---

## Notes for recording

- Use OBS or similar at 1920×1080
- Record audio separately from microphone, mix in post if needed
- The `--no-publish` flag is safe to demo on any machine with no broker required
- Do NOT show any real IP addresses, real credentials, or real client device serial numbers on screen — only localhost (127.0.0.1) and the public HiveMQ broker
