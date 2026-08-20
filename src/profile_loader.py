"""
src/profile_loader.py
----------------------
Loads and validates device YAML profiles from config/devices/*.yaml.

Design Requirements:
- Fail loudly (raise, never silently default) on invalid or incomplete profiles.
  A bad profile must be rejected at startup, not discovered mid-poll-cycle.
- byte_order must be one of the explicitly supported values.
- data_type must be one of the explicitly supported values.
- Device-specific logic lives entirely in the YAML. This module is generic.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from src.byte_order import SUPPORTED_BYTE_ORDERS

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported field values — extend here when adding new modes to byte_order.py
# ---------------------------------------------------------------------------
SUPPORTED_DATA_TYPES: frozenset[str] = frozenset({"float32", "uint16", "int16"})

#: 'holding' = FC 0x03 (most devices); 'input' = FC 0x04 (e.g. Selec EM2M-1P)
SUPPORTED_REGISTER_TYPES: frozenset[str] = frozenset({"holding", "input"})

# Required top-level keys in every profile
_REQUIRED_PROFILE_KEYS: tuple[str, ...] = (
    "device_name",
    "slave_id",
    "poll_interval_seconds",
    "registers",
)

# Required keys in every register entry
_REQUIRED_REGISTER_KEYS: tuple[str, ...] = (
    "name",
    "address",
    "register_count",
    "data_type",
    "byte_order",
    "unit",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class ProfileValidationError(ValueError):
    """Raised when a device profile fails validation.

    This is a ValueError subclass so callers can catch it specifically or
    let it propagate as a generic ValueError.
    """


def load_profile(path: Path) -> dict[str, Any]:
    """Load and validate a single device YAML profile.

    Args:
        path: Path to a .yaml device profile file.

    Returns:
        The validated profile dict. The returned dict is a plain Python dict
        suitable for direct use by modbus_client.py and edge_rules.py.

    Raises:
        FileNotFoundError: If the path does not exist.
        ProfileValidationError: If the profile fails any validation check.
        yaml.YAMLError: If the file is not valid YAML.
    """
    log.debug("Loading profile: %s", path)

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ProfileValidationError(
            f"{path}: Profile must be a YAML mapping, got {type(raw).__name__}"
        )

    _validate_top_level(raw, path)
    _validate_registers(raw["registers"], path)

    log.info(
        "Loaded profile: %s  (slave_id=%s, %d register(s))",
        raw["device_name"],
        raw["slave_id"],
        len(raw["registers"]),
    )
    return raw


def load_all_profiles(config_dir: Path) -> list[dict[str, Any]]:
    """Load all *.yaml files from config_dir as device profiles.

    Files that fail validation are logged as errors and skipped — one bad
    profile must not prevent other devices from being polled.

    Args:
        config_dir: Directory containing *.yaml profile files.

    Returns:
        List of validated profile dicts. May be empty if no valid profiles found.
    """
    yaml_files = sorted(config_dir.glob("*.yaml"))
    if not yaml_files:
        log.warning("No *.yaml profile files found in %s", config_dir)
        return []

    profiles: list[dict[str, Any]] = []
    for yaml_path in yaml_files:
        try:
            profiles.append(load_profile(yaml_path))
        except (ProfileValidationError, yaml.YAMLError, OSError) as exc:
            log.error("Skipping profile %s — validation failed: %s", yaml_path.name, exc)

    return profiles


# ---------------------------------------------------------------------------
# Internal validators
# ---------------------------------------------------------------------------

def _validate_top_level(profile: dict[str, Any], path: Path) -> None:
    """Check that all required top-level keys are present and have valid types."""
    missing = [k for k in _REQUIRED_PROFILE_KEYS if k not in profile]
    if missing:
        raise ProfileValidationError(
            f"{path}: Missing required top-level keys: {missing}"
        )

    if not isinstance(profile["device_name"], str) or not profile["device_name"].strip():
        raise ProfileValidationError(
            f"{path}: 'device_name' must be a non-empty string"
        )

    try:
        slave_id = int(profile["slave_id"])
        if not (1 <= slave_id <= 247):
            raise ValueError
    except (ValueError, TypeError):
        raise ProfileValidationError(
            f"{path}: 'slave_id' must be an integer in range 1–247, "
            f"got {profile['slave_id']!r}"
        )

    try:
        interval = float(profile["poll_interval_seconds"])
        if interval <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise ProfileValidationError(
            f"{path}: 'poll_interval_seconds' must be a positive number, "
            f"got {profile['poll_interval_seconds']!r}"
        )

    if not isinstance(profile["registers"], list) or not profile["registers"]:
        raise ProfileValidationError(
            f"{path}: 'registers' must be a non-empty list"
        )

    # register_type is optional; defaults to 'holding' (FC=0x03)
    reg_type = profile.get("register_type", "holding")
    if reg_type not in SUPPORTED_REGISTER_TYPES:
        raise ProfileValidationError(
            f"{path}: 'register_type' is '{reg_type}', "
            f"must be one of {sorted(SUPPORTED_REGISTER_TYPES)}"
        )


def _validate_registers(registers: list[Any], path: Path) -> None:
    """Validate each register entry in the profile."""
    seen_names: set[str] = set()

    for i, reg in enumerate(registers):
        if not isinstance(reg, dict):
            raise ProfileValidationError(
                f"{path}: registers[{i}] must be a mapping, got {type(reg).__name__}"
            )

        missing = [k for k in _REQUIRED_REGISTER_KEYS if k not in reg]
        if missing:
            raise ProfileValidationError(
                f"{path}: registers[{i}] missing required keys: {missing}"
            )

        name = reg["name"]
        if not isinstance(name, str) or not name.strip():
            raise ProfileValidationError(
                f"{path}: registers[{i}]['name'] must be a non-empty string"
            )
        if name in seen_names:
            raise ProfileValidationError(
                f"{path}: Duplicate register name '{name}' — names must be unique per device"
            )
        seen_names.add(name)

        byte_order = reg["byte_order"]
        if byte_order not in SUPPORTED_BYTE_ORDERS:
            raise ProfileValidationError(
                f"{path}: registers['{name}']['byte_order'] is '{byte_order}', "
                f"must be one of {sorted(SUPPORTED_BYTE_ORDERS)}"
            )

        data_type = reg["data_type"]
        if data_type not in SUPPORTED_DATA_TYPES:
            raise ProfileValidationError(
                f"{path}: registers['{name}']['data_type'] is '{data_type}', "
                f"must be one of {sorted(SUPPORTED_DATA_TYPES)}"
            )

        # register_count must match data_type expectation
        expected_count = 2 if data_type == "float32" else 1
        actual_count = reg["register_count"]
        if int(actual_count) != expected_count:
            raise ProfileValidationError(
                f"{path}: registers['{name}']['register_count'] should be "
                f"{expected_count} for data_type='{data_type}', got {actual_count}"
            )

        # address must be a non-negative integer
        try:
            addr = int(reg["address"])
            if addr < 0:
                raise ValueError
        except (ValueError, TypeError):
            raise ProfileValidationError(
                f"{path}: registers['{name}']['address'] must be a non-negative integer"
            )

        # alert_range, if present, must be a [min, max] pair
        if "alert_range" in reg:
            ar = reg["alert_range"]
            if (
                not isinstance(ar, (list, tuple))
                or len(ar) != 2
                or not all(isinstance(v, (int, float)) for v in ar)
            ):
                raise ProfileValidationError(
                    f"{path}: registers['{name}']['alert_range'] must be a "
                    f"[min, max] pair of numbers, got {ar!r}"
                )
            if float(ar[0]) > float(ar[1]):
                raise ProfileValidationError(
                    f"{path}: registers['{name}']['alert_range'] min ({ar[0]}) "
                    f"must be <= max ({ar[1]})"
                )
