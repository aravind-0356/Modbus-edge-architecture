# Ledger: Resilient Industrial Edge Gateway

![Status](https://img.shields.io/badge/Status-Production_Ready-success)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![License](https://img.shields.io/badge/License-MIT-green)

Ledger is an open-source, resilient software edge layer designed to sit between raw industrial hardware (Modbus RTU/TCP) and modern cloud architectures (MQTT/IoT). 

It deliberately decouples **hardware translation** from **data intelligence**, allowing you to use cheap, off-the-shelf, vendor-neutral hardware while handling device profiles, network resilience, and data normalization entirely in software.

*(Note for Portfolio: Insert a GIF or screenshot of the dark-mode dashboard here)*
`![Ledger Dashboard](docs/assets/dashboard_preview.png)`

---

## ⚠️ The Problem with Standard Integrations

Most industrial IoT integrations rely on expensive, proprietary edge gateways (e.g., Siemens, groov EPIC, or proprietary cloud boxes). These solutions introduce three major problems:
1. **Vendor Lock-in:** You are locked into their hardware ecosystem and cloud pricing.
2. **Brittle Architectures:** If a sensor changes from a Selec meter to a Schneider meter, you often have to rewrite hardcoded PLC logic or gateway scripts.
3. **Data Loss:** Basic gateways push data directly to the cloud. If the factory internet drops for 10 minutes, you permanently lose 10 minutes of critical operational data.

## 💡 The Ledger Architecture

Ledger solves this by pushing the intelligence to a local software layer (running on an Edge PC, Raspberry Pi, or Industrial IPC) and treating the hardware as disposable.

*(Note for Portfolio: Insert your Altium Schematic here)*
`![Architecture Schematic](docs/wiring_schematic_v4.png)`

1. **Dumb Hardware:** We use inexpensive, vendor-neutral Serial-to-Ethernet servers (like the USR-DR404) simply to convert RS-485 electrical signals to TCP packets.
2. **Smart Software (Ledger):** The Python-based Edge Engine polls the raw registers, normalizes them into engineering units using human-readable YAML profiles, and publishes them as clean JSON.
3. **Store & Forward Resilience:** Every single reading is written to a local SQLite buffer **first**. If the cloud connection drops, polling continues and the local database fills up. Upon reconnection, Ledger replays the missing data to the cloud with the exact original timestamps. **Zero data loss.**

---

## ⚙️ Core Capabilities

1. **Device Knowledge:** Turns raw, unlabeled register values into named, scaled, real-world quantities (`"voltage": 230.4, "unit": "V"`, not `"register_0": 2300`).
2. **Multi-Vendor Normalization:** Multiple field devices from different manufacturers, each with a completely different register layout, byte order, and scaling convention, produce a single unified JSON output stream. 
3. **Edge Anomaly Detection:** Threshold checks and immediate alerting run at the edge (`edge_rules.py`), catching physical anomalies (e.g., cavitation, blockages) before the data even reaches the cloud.
4. **Hybrid Edge-to-Cloud Ready:** By simply updating the MQTT broker IP in the configuration, this system instantly routes normalized data to enterprise clouds like **AWS IoT Core**, **Azure IoT Hub**, or **HiveMQ Cloud**.
5. **Globally Accessible Dashboard:** The included HTML/JS dashboard is a standalone static web app. It can run completely offline as a "Local HMI" on the factory floor, or be dragged and dropped into **AWS S3**, **Vercel**, or **GitHub Pages** for real-time global monitoring from any browser in the world.

---

## 🚀 Quick Start (Simulation Mode)

You can run the entire stack locally without any physical Modbus hardware using the included simulators.

### 1. Install Dependencies
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```
> **Note**: `requirements.txt` pins `pymodbus==3.6.9`. pymodbus 3.14+ has a breaking datastore API change that affects the simulator. The `src/` polling code works with 3.6.9 and later, but the simulator requires 3.6.9 specifically.

### 2. Connection Configuration
Copy `config/local.yaml.example` to `config/local.yaml` and fill in your gateway IP, port, and MQTT broker details. *(Note: `config/local.yaml` is gitignored — do not commit it).*

### 3. Start the Hardware Simulators
Open a terminal and start the simulated physical devices:
```bash
# Terminal 1 — start simulated Modbus slave A
python simulators/modbus_slave_sim.py --config config/devices/sim_device_a.yaml

# Terminal 2 — start simulated Modbus slave B
python simulators/modbus_slave_sim.py --config config/devices/sim_device_b.yaml
```

### 4. Start the Ledger Edge Engine
In a new terminal, run the main orchestration loop:
```bash
python src/main.py --config-dir config/devices/
```

### 5. Open the Dashboard
Simply double-click `dashboard/index.html` in your web browser. You will see real-time data streaming from the simulators, and you can test the "Store & Forward" resilience by temporarily stopping your local MQTT broker connection.

---

## 🛠️ Adding a New Field Device

To add a new pump, flow meter, or VFD, just create a new file in `config/devices/` following the existing schema. No Python code changes required.

```yaml
# config/devices/water_pump_01.yaml
device_name: "WaterPump_01"
slave_id: 3
ip_address: "192.168.1.100"
port: 502
poll_interval_seconds: 5.0
registers:
  - name: "Discharge_Pressure"
    address: 40001
    type: "holding"
    data_type: "float32"
    byte_order: "CDAB"      # Handles weird vendor endianness seamlessly
    scale: 0.1
    unit: "PSI"
```

## 📄 License & Custom Integration
This core architecture is open-source under the MIT License. 

**Looking for a custom industrial integration?** This architecture can be rapidly adapted to specific factory requirements—whether that means writing control logic back to PLCs, pushing normalized telemetry to AWS IoT Core / Azure IoT Hub, or bridging legacy equipment into a modern SCADA system. 

Feel free to fork this repository or reach out for custom integration services.
