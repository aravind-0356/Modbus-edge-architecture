"""
src/modbus_client.py
---------------------
Modbus TCP polling client.

Speaks Modbus TCP to the gateway (which handles RTU↔TCP conversion).
Never implements RTU framing — that's the gateway's job (SKILLS.md).
Supports both holding registers (FC=0x03) and input registers (FC=0x04),
selected via the profile's 'register_type' field.

Per AGENTS.md: failures in one device's poll cycle must not affect others.
This module raises exceptions on failure; the caller (main.py) wraps each
device in its own try/except.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.byte_order import decode_registers
from src.profile_loader import load_profile  # noqa: F401  (re-exported for convenience)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# pymodbus import — handle 3.x API
# ---------------------------------------------------------------------------
try:
    from pymodbus.client import ModbusTcpClient
    from pymodbus.exceptions import ModbusException
except ImportError as exc:
    raise ImportError(
        "pymodbus is required. Install it with: pip install pymodbus"
    ) from exc


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Reading:
    """One normalized field reading from one poll cycle.

    Attributes:
        device_name:   From the device profile's 'device_name' key.
        timestamp_utc: UTC timestamp of when the register was read (not when published).
        field_name:    Register name from the profile (e.g. 'voltage').
        value:         Decoded, scaled real-world value.
        unit:          Unit string from the profile (e.g. 'V', 'A', 'W').
        raw_registers: The original 16-bit register words, preserved for debugging.
        alert:         True if the value falls outside the profile's alert_range.
        alert_message: Human-readable alert description, or empty string.
    """
    device_name:   str
    timestamp_utc: datetime
    field_name:    str
    value:         float
    unit:          str
    raw_registers: list[int] = field(default_factory=list)
    alert:         bool = False
    alert_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for JSON output or MQTT payload."""
        return {
            "device_name":   self.device_name,
            "timestamp_utc": self.timestamp_utc.isoformat(),
            "field_name":    self.field_name,
            "value":         self.value,
            "unit":          self.unit,
            "alert":         self.alert,
            "alert_message": self.alert_message,
        }


# ---------------------------------------------------------------------------
# Polling function
# ---------------------------------------------------------------------------

def poll_device(
    host: str,
    port: int,
    profile: dict[str, Any],
    timeout: float = 5.0,
) -> list[Reading]:
    """Poll all registers defined in a device profile over Modbus TCP.

    Connects to the Modbus TCP gateway, reads each register group declared in
    the profile, decodes the raw words using the profile's byte_order and
    data_type, applies scaling, and returns a list of Reading objects.

    The TCP connection is opened and closed once per call. On a shared
    gateway serving many slave devices, this is appropriate — each call
    polls one slave_id.

    Args:
        host:     IP address or hostname of the Modbus TCP gateway.
        port:     TCP port of the gateway (default is 502 for real gateways;
                  5020+ for the simulator).
        profile:  Validated device profile dict from profile_loader.load_profile().
        timeout:  Socket timeout in seconds.

    Returns:
        List of Reading objects — one per successfully read register.
        Registers that fail individually are logged and skipped; they do not
        cause the entire device poll to fail.

    Raises:
        ConnectionError: If the TCP connection to the gateway cannot be established.
        ModbusException: If the gateway returns a Modbus exception response
                         for the entire connection (not per-register errors).
    """
    device_name  = profile["device_name"]
    slave_id     = int(profile["slave_id"])
    register_type = profile.get("register_type", "holding")
    registers_cfg = profile["registers"]

    log.debug("Connecting to %s:%d for device '%s' (slave_id=%d)", host, port, device_name, slave_id)

    client = ModbusTcpClient(host, port=port, timeout=timeout)
    connected = client.connect()
    if not connected:
        raise ConnectionError(
            f"Could not connect to Modbus TCP gateway at {host}:{port} "
            f"for device '{device_name}'"
        )

    readings: list[Reading] = []
    poll_time = datetime.now(tz=timezone.utc)

    try:
        for reg_cfg in registers_cfg:
            name           = reg_cfg["name"]
            address        = int(reg_cfg["address"])
            register_count = int(reg_cfg["register_count"])
            data_type      = reg_cfg["data_type"]
            byte_order     = reg_cfg["byte_order"]
            scale          = float(reg_cfg.get("scale", 1.0))
            unit           = reg_cfg.get("unit", "")
            alert_range    = reg_cfg.get("alert_range")

            try:
                result = _read_registers(
                    client, register_type, address, register_count, slave_id
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "[%s] Failed to read register '%s' @ address 0x%04X: %s",
                    device_name, name, address, exc,
                )
                continue

            raw_regs = list(result.registers)
            try:
                raw_value = decode_registers(raw_regs, byte_order, data_type)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "[%s] Failed to decode register '%s': %s",
                    device_name, name, exc,
                )
                continue

            value = float(raw_value) * scale

            alert, alert_msg = _check_alert(name, value, alert_range)
            if alert:
                log.warning("[%s] ALERT: %s", device_name, alert_msg)

            readings.append(Reading(
                device_name=device_name,
                timestamp_utc=poll_time,
                field_name=name,
                value=round(value, 6),
                unit=unit,
                raw_registers=raw_regs,
                alert=alert,
                alert_message=alert_msg,
            ))

            log.debug(
                "[%s] %s = %.4f %s  (raw: %s)",
                device_name, name, value, unit,
                [f"0x{r:04X}" for r in raw_regs],
            )

    finally:
        client.close()

    log.info(
        "[%s] Polled %d/%d register(s) successfully",
        device_name, len(readings), len(registers_cfg),
    )
    return readings


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_registers(
    client: "ModbusTcpClient",
    register_type: str,
    address: int,
    count: int,
    slave_id: int,
) -> Any:
    """Read registers using the correct function code for the register type.

    Args:
        client:        Connected pymodbus ModbusTcpClient.
        register_type: 'holding' (FC=0x03) or 'input' (FC=0x04).
        address:       Starting register address (0-based).
        count:         Number of 16-bit registers to read.
        slave_id:      Modbus slave ID.

    Returns:
        pymodbus response object with a .registers attribute.

    Raises:
        ValueError:     If register_type is not supported.
        ModbusException: If the response is a Modbus exception.
        Exception:      For connection/timeout errors.
    """
    if register_type == "holding":
        result = client.read_holding_registers(address, count, slave=slave_id)
    elif register_type == "input":
        result = client.read_input_registers(address, count, slave=slave_id)
    else:
        raise ValueError(f"Unsupported register_type: '{register_type}'")

    if result.isError():
        raise ModbusException(
            f"Modbus error response for address=0x{address:04X} "
            f"count={count} slave={slave_id}: {result}"
        )
    return result


def _check_alert(
    field_name: str,
    value: float,
    alert_range: list[float] | None,
) -> tuple[bool, str]:
    """Check whether a value falls outside the alert range.

    Returns:
        (is_alert, message) — message is empty string if no alert.
    """
    if alert_range is None:
        return False, ""
    low, high = float(alert_range[0]), float(alert_range[1])
    if value < low:
        return True, f"{field_name}={value:.4f} is below minimum {low}"
    if value > high:
        return True, f"{field_name}={value:.4f} is above maximum {high}"
    return False, ""
