"""
simulators/modbus_slave_sim.py
-------------------------------
A configurable Modbus TCP slave simulator built on pymodbus.

Purpose: provide a real Modbus TCP endpoint to develop and test against
when no physical gateway or field device is available. All data produced
by this simulator is SIMULATED — it must not be presented as real hardware
data anywhere in docs, demo videos, or the case study.

Usage:
    python simulators/modbus_slave_sim.py --config config/devices/sim_device_a.yaml
    python simulators/modbus_slave_sim.py --config config/devices/sim_device_b.yaml

Each invocation starts one Modbus TCP server bound to the host/port declared
in the YAML profile (under the optional `simulator` key). Multiple instances
can run concurrently — start each in its own terminal.

YAML simulator block (optional, defaults shown):
    simulator:
      host: "127.0.0.1"
      port: 5020
      update_interval_seconds: 2   # how often register values drift/update
"""

from __future__ import annotations

import argparse
import logging
import math
import struct
import sys
import threading
import time
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# pymodbus 3.6.x imports (pinned via requirements.txt to pymodbus==3.6.9)
# ---------------------------------------------------------------------------
try:
    from pymodbus.datastore import (
        ModbusSequentialDataBlock,
        ModbusServerContext,
        ModbusSlaveContext,
    )
    from pymodbus.server import StartTcpServer
except ImportError:
    print("ERROR: pymodbus 3.6.9 is required. Run: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [SIM]  %(levelname)s  %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Byte-packing helpers
# These mirror the modes in src/byte_order.py so the simulator can populate
# registers using the same encoding the parser will expect to decode.
# ---------------------------------------------------------------------------

def pack_float32_big_endian(value: float) -> list[int]:
    """Pack a float32 into two 16-bit registers, big-endian word order.

    Register[0] = high word, Register[1] = low word.
    Byte order within each register is also big-endian (network byte order).
    This matches the 'big_endian' mode in src/byte_order.py.
    """
    raw = struct.pack(">f", value)          # 4 bytes, big-endian IEEE-754
    high = struct.unpack(">H", raw[0:2])[0]
    low  = struct.unpack(">H", raw[2:4])[0]
    return [high, low]


def pack_float32_little_endian(value: float) -> list[int]:
    """Pack a float32 into two 16-bit registers, little-endian word order.

    Register[0] = low word, Register[1] = high word.
    Byte order within each register is little-endian.
    This matches the 'little_endian' mode in src/byte_order.py.
    """
    raw = struct.pack("<f", value)          # 4 bytes, little-endian IEEE-754
    low  = struct.unpack("<H", raw[0:2])[0]
    high = struct.unpack("<H", raw[2:4])[0]
    return [low, high]


def pack_float32_big_endian_word_swap(value: float) -> list[int]:
    """Pack a float32 into two 16-bit registers, big-endian bytes, word-swapped.

    Register[0] = low word, Register[1] = high word
    (word order is swapped vs. standard big-endian, but bytes within each
    register remain big-endian). Common on some energy meters and PLCs.
    This matches the 'big_endian_word_swap' mode in src/byte_order.py.
    """
    raw = struct.pack(">f", value)          # 4 bytes, big-endian IEEE-754
    high = struct.unpack(">H", raw[0:2])[0]
    low  = struct.unpack(">H", raw[2:4])[0]
    return [low, high]                      # words swapped


def pack_uint16(value: int) -> list[int]:
    """Pack a single 16-bit unsigned integer into one register."""
    return [int(value) & 0xFFFF]


PACK_FUNCTIONS = {
    "big_endian":            pack_float32_big_endian,
    "little_endian":         pack_float32_little_endian,
    "big_endian_word_swap":  pack_float32_big_endian_word_swap,
}


# ---------------------------------------------------------------------------
# Register block builder
# ---------------------------------------------------------------------------

def build_register_block(registers: list[dict], sim_values: dict[str, float]) -> dict[int, int]:
    """Build a flat address → register-value mapping from a profile's register list.

    Args:
        registers:  The `registers` list from a device YAML profile.
        sim_values: Optional override dict mapping field name → float value.
                    Fields not present here receive their `sim_default` or 0.0.

    Returns:
        A dict mapping each Modbus register address (int) to its 16-bit value (int).
    """
    block: dict[int, int] = {}
    for reg in registers:
        name      = reg["name"]
        address   = reg["address"]
        data_type = reg.get("data_type", "float32")
        byte_order = reg.get("byte_order", "big_endian")
        scale     = float(reg.get("scale", 1.0))
        default   = float(reg.get("sim_default", 0.0))

        raw_value = sim_values.get(name, default)

        if data_type == "float32":
            pack_fn = PACK_FUNCTIONS.get(byte_order)
            if pack_fn is None:
                log.warning("Unknown byte_order '%s' for field '%s', defaulting to big_endian", byte_order, name)
                pack_fn = pack_float32_big_endian
            words = pack_fn(raw_value / scale if scale != 1.0 else raw_value)
        elif data_type in ("uint16", "int16"):
            words = pack_uint16(int(raw_value / scale) if scale != 1.0 else int(raw_value))
        else:
            log.warning("Unsupported data_type '%s' for field '%s', skipping", data_type, name)
            continue

        for offset, word in enumerate(words):
            block[address + offset] = word

    return block


# ---------------------------------------------------------------------------
# Simulated value generator — produces slowly drifting realistic values
# ---------------------------------------------------------------------------

class ValueGenerator:
    """Produces slowly oscillating simulated values for a set of register fields.

    Each field drifts sinusoidally within ±drift_pct of its centre value.
    This keeps the simulator values moving on the dashboard without any real sensor.
    These values are SIMULATED and must be labeled as such in any demo output.
    """

    def __init__(self, registers: list[dict], update_interval: float) -> None:
        self._registers = registers
        self._update_interval = update_interval
        self._start_time = time.monotonic()
        # Each field gets a slightly different phase so they don't all move together
        self._phases: dict[str, float] = {
            reg["name"]: i * 0.7 for i, reg in enumerate(registers)
        }

    def current_values(self) -> dict[str, float]:
        """Return a dict of field_name → current simulated float value."""
        t = time.monotonic() - self._start_time
        values: dict[str, float] = {}
        for reg in self._registers:
            name    = reg["name"]
            centre  = float(reg.get("sim_default", 0.0))
            drift   = float(reg.get("sim_drift_pct", 2.0)) / 100.0
            phase   = self._phases[name]
            values[name] = centre * (1.0 + drift * math.sin(t / 10.0 + phase))
        return values


# ---------------------------------------------------------------------------
# Register updater thread — keeps the datastore fresh
# ---------------------------------------------------------------------------

class RegisterUpdater(threading.Thread):
    """Background thread that periodically rewrites the Modbus datastore.

    This gives the simulator "live" drifting values rather than static registers,
    making it more realistic as a demo target.
    """

    def __init__(
        self,
        context: "ModbusServerContext",
        slave_id: int,
        registers: list[dict],
        generator: ValueGenerator,
        update_interval: float,
    ) -> None:
        super().__init__(daemon=True)
        self._context = context
        self._slave_id = slave_id
        self._registers = registers
        self._generator = generator
        self._update_interval = update_interval

    def run(self) -> None:
        while True:
            try:
                values = self._generator.current_values()
                block  = build_register_block(self._registers, values)

                if not block:
                    time.sleep(self._update_interval)
                    continue

                max_addr = max(block.keys())
                # Build a flat list from address 0 to max_addr (gaps filled with 0)
                flat = [block.get(a, 0) for a in range(max_addr + 1)]

                # pymodbus slave context: function code 3 = holding registers
                self._context[self._slave_id].setValues(3, 0, flat)

                log.debug(
                    "slave_id=%d  Updated %d register(s). Sample: %s",
                    self._slave_id,
                    len(block),
                    {k: hex(v) for k, v in list(block.items())[:4]},
                )
            except Exception as exc:  # noqa: BLE001
                log.error("RegisterUpdater error: %s", exc)

            time.sleep(self._update_interval)


# ---------------------------------------------------------------------------
# Profile loader (minimal — src/profile_loader.py does the full version)
# ---------------------------------------------------------------------------

def load_profile(config_path: Path) -> dict:
    """Load and minimally validate a device YAML profile for the simulator."""
    with config_path.open("r", encoding="utf-8") as fh:
        profile = yaml.safe_load(fh)

    required = ("device_name", "slave_id", "registers")
    missing = [k for k in required if k not in profile]
    if missing:
        raise ValueError(f"Profile {config_path} is missing required keys: {missing}")

    if not isinstance(profile["registers"], list) or not profile["registers"]:
        raise ValueError(f"Profile {config_path}: 'registers' must be a non-empty list")

    return profile


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Modbus TCP slave simulator. Simulates a field device "
                    "with register values derived from a YAML profile. "
                    "ALL DATA IS SIMULATED."
    )
    parser.add_argument(
        "--config",
        required=True,
        type=Path,
        help="Path to device YAML profile (e.g. config/devices/sim_device_a.yaml)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Override simulator bind host (default: profile's simulator.host or 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override simulator TCP port (default: profile's simulator.port or 5020)",
    )
    args = parser.parse_args()

    profile = load_profile(args.config)
    sim_cfg  = profile.get("simulator", {})

    host             = args.host or sim_cfg.get("host", "127.0.0.1")
    port             = args.port or sim_cfg.get("port", 5020)
    update_interval  = float(sim_cfg.get("update_interval_seconds", 2.0))
    slave_id         = int(profile["slave_id"])
    device_name      = profile["device_name"]
    registers        = profile["registers"]

    log.info("=" * 60)
    log.info("Modbus TCP Slave Simulator — ALL DATA IS SIMULATED")
    log.info("Device  : %s", device_name)
    log.info("Slave ID: %d", slave_id)
    log.info("Binding : %s:%d", host, port)
    log.info("Fields  : %s", [r["name"] for r in registers])
    log.info("=" * 60)

    # Build initial register values
    generator = ValueGenerator(registers, update_interval)
    initial_values = generator.current_values()
    initial_block  = build_register_block(registers, initial_values)

    # Determine how large the holding register block needs to be
    if not initial_block:
        log.error("No registers could be packed. Check your YAML profile.")
        sys.exit(1)

    max_addr = max(initial_block.keys())
    flat_initial = [initial_block.get(a, 0) for a in range(max_addr + 1)]

    log.info("Initializing %d holding register(s) (addresses 0–%d)", max_addr + 1, max_addr)

    # Build pymodbus data store
    holding_block = ModbusSequentialDataBlock(0, flat_initial + [0] * 10)  # small headroom
    slave_context = ModbusSlaveContext(
        di=ModbusSequentialDataBlock(0, [0] * 10),   # discrete inputs
        co=ModbusSequentialDataBlock(0, [0] * 10),   # coils
        hr=holding_block,                             # holding registers (FC=3)
        ir=ModbusSequentialDataBlock(0, [0] * 10),   # input registers
    )
    server_context = ModbusServerContext(slaves={slave_id: slave_context}, single=False)

    # Start background thread that keeps register values updated
    updater = RegisterUpdater(
        context=server_context,
        slave_id=slave_id,
        registers=registers,
        generator=generator,
        update_interval=update_interval,
    )
    updater.start()
    log.info("Register updater started (interval: %.1fs)", update_interval)

    # Start Modbus TCP server (blocks until Ctrl-C)
    log.info("Server starting — press Ctrl-C to stop")
    try:
        StartTcpServer(context=server_context, address=(host, port))
    except KeyboardInterrupt:
        log.info("Simulator stopped by user.")
    except OSError as exc:
        log.error("Could not bind to %s:%d — %s", host, port, exc)
        log.error("Is another simulator already running on this port?")
        sys.exit(1)


if __name__ == "__main__":
    main()
