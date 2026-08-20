"""
src/edge_rules.py
------------------
Threshold and anomaly checks derived from device profiles.

The alert_range for each register field is defined in the YAML profile —
never hardcoded here. This module is generic: it reads thresholds from
whatever profile it's given.

Alert readings get an immediate, priority publish path (they don't wait
for the normal batch interval). This is the "edge intelligence" demo point.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def check_reading_alert(
    field_name: str,
    value: float,
    register_config: dict[str, Any],
) -> tuple[bool, str]:
    """Check whether a single reading value violates its alert_range.

    The alert_range is read from the register's profile entry — this function
    never contains device-specific logic.

    Args:
        field_name:      Name of the register (used in the alert message).
        value:           Decoded, scaled real-world value.
        register_config: The register entry dict from the device profile.

    Returns:
        (is_alert: bool, message: str)
        message is an empty string when is_alert is False.
    """
    alert_range = register_config.get("alert_range")
    if alert_range is None:
        return False, ""

    lo, hi = float(alert_range[0]), float(alert_range[1])
    unit = register_config.get("unit", "")
    unit_str = f" {unit}" if unit else ""

    if value < lo:
        msg = (
            f"ALERT: {field_name} = {value:.4f}{unit_str} "
            f"is BELOW minimum threshold {lo}{unit_str}"
        )
        return True, msg

    if value > hi:
        msg = (
            f"ALERT: {field_name} = {value:.4f}{unit_str} "
            f"is ABOVE maximum threshold {hi}{unit_str}"
        )
        return True, msg

    return False, ""


def evaluate_readings(
    readings: list[Any],
    profile: dict[str, Any],
) -> tuple[list[Any], list[Any]]:
    """Split a list of Reading objects into normal and alert groups.

    Alert readings are returned separately so the caller can dispatch them
    on an immediate publish path rather than the normal batch interval.

    Args:
        readings: List of src.modbus_client.Reading objects from poll_device().
        profile:  The validated device profile dict. Used to look up alert_range
                  per field from the registers list.

    Returns:
        (normal_readings, alert_readings): Two lists — alert readings also appear
        in the Reading objects with .alert=True and .alert_message set.
        Note: alert readings are NOT included in normal_readings.
    """
    # Build a lookup from field name → register config
    reg_by_name: dict[str, dict[str, Any]] = {
        r["name"]: r for r in profile.get("registers", [])
    }

    normal: list[Any] = []
    alerts: list[Any] = []

    for reading in readings:
        reg_cfg = reg_by_name.get(reading.field_name, {})
        is_alert, msg = check_reading_alert(reading.field_name, reading.value, reg_cfg)

        # Update the Reading object's alert fields in-place
        reading.alert = is_alert
        reading.alert_message = msg

        if is_alert:
            log.warning(
                "[%s] %s", reading.device_name, msg
            )
            alerts.append(reading)
        else:
            normal.append(reading)

    return normal, alerts


def build_alert_summary(alert_readings: list[Any]) -> str:
    """Build a human-readable multi-line summary of active alerts.

    Useful for logging or a status endpoint.
    """
    if not alert_readings:
        return "No active alerts."
    lines = [f"  • {r.alert_message}" for r in alert_readings]
    return f"{len(alert_readings)} alert(s):\n" + "\n".join(lines)
