"""
src/byte_order.py
------------------
Register-to-value conversion for all supported Modbus byte-order modes.

This is the highest-risk silent-failure point in Modbus integration work:
a wrong byte-order assumption produces a plausible-looking but wrong number
with no error or exception. Each mode is therefore implemented as a separate,
explicitly-named function — no "smart" auto-detection, which would hide bugs.

Supported modes
---------------
  big_endian             — Standard word order, standard byte order within word.
                           Register[0] = high word, Register[1] = low word.
                           Bytes within each register: big-endian (network byte order).

  little_endian          — Register[0] = low word, Register[1] = high word.
                           Bytes within each register: little-endian.

  big_endian_word_swap   — Byte order within registers is big-endian, but the
                           two registers are swapped relative to standard big-endian:
                           Register[0] = low word, Register[1] = high word.
                           Common in Selec energy meters and some other vendors.

Testing Requirements
--------------------------------
Every function here must have a corresponding unit test with a known
input/output pair sourced from real datasheet data or IEEE-754 arithmetic —
not fabricated. See tests/test_byte_order.py.
"""

from __future__ import annotations

import struct
from typing import Sequence


# ---------------------------------------------------------------------------
# Type alias
# ---------------------------------------------------------------------------
# A raw register value is a 16-bit unsigned integer.
RegisterWord = int


# ---------------------------------------------------------------------------
# Public API — one function per mode
# ---------------------------------------------------------------------------

def registers_to_float32_big_endian(registers: Sequence[RegisterWord]) -> float:
    """Convert two 16-bit registers to a float32, big-endian word and byte order.

    Register layout:
        registers[0] = high word (most significant 16 bits)
        registers[1] = low  word (least significant 16 bits)

    Byte order within each register: big-endian (network byte order).

    This is the "textbook" big-endian interpretation and is used by devices
    that follow the most common convention.

    Args:
        registers: Sequence of exactly 2 unsigned 16-bit integers as returned
                   by pymodbus (e.g. result.registers).

    Returns:
        The decoded float32 value.

    Raises:
        ValueError: If registers does not contain exactly 2 elements.
        struct.error: If the packed bytes cannot be unpacked as a float32.

    IEEE-754 derivation example (used in tests/test_byte_order.py):
        value    = 123.456
        hex repr = 0x42F6E979
        high     = 0x42F6  → registers[0]
        low      = 0xE979  → registers[1]
    """
    _require_count(registers, 2)
    high, low = int(registers[0]) & 0xFFFF, int(registers[1]) & 0xFFFF
    raw = struct.pack(">HH", high, low)
    return struct.unpack(">f", raw)[0]


def registers_to_float32_little_endian(registers: Sequence[RegisterWord]) -> float:
    """Convert two 16-bit registers to a float32, little-endian word and byte order.

    Register layout:
        registers[0] = low  word (least significant 16 bits)
        registers[1] = high word (most significant 16 bits)

    Byte order within each register: little-endian.

    Args:
        registers: Sequence of exactly 2 unsigned 16-bit integers.

    Returns:
        The decoded float32 value.

    Raises:
        ValueError: If registers does not contain exactly 2 elements.

    IEEE-754 derivation example (used in tests/test_byte_order.py):
        value    = 123.456
        hex repr = 0x42F6E979 (big-endian canonical form)
        little-endian bytes: 79 E9 F6 42
        low  word (LE) = 0xE979  → registers[0]  (bytes: 79 E9)
        high word (LE) = 0x42F6  → registers[1]  (bytes: F6 42)
        NOTE: within each word the bytes are also little-endian, so
        registers[0] as a LE uint16 holding bytes [79, E9] = 0xE979
    """
    _require_count(registers, 2)
    low, high = int(registers[0]) & 0xFFFF, int(registers[1]) & 0xFFFF
    raw = struct.pack("<HH", low, high)
    return struct.unpack("<f", raw)[0]


def registers_to_float32_big_endian_word_swap(registers: Sequence[RegisterWord]) -> float:
    """Convert two 16-bit registers to a float32, big-endian bytes, word-swapped.

    Register layout:
        registers[0] = low  word (least significant 16 bits)
        registers[1] = high word (most significant 16 bits)

    Byte order within each register: big-endian.

    This is NOT standard big-endian: the high word and low word registers are
    swapped relative to the standard big-endian layout. This mode is common in
    Selec energy meters, some Schneider and ABB devices.

    Args:
        registers: Sequence of exactly 2 unsigned 16-bit integers.

    Returns:
        The decoded float32 value.

    Raises:
        ValueError: If registers does not contain exactly 2 elements.

    IEEE-754 derivation example (used in tests/test_byte_order.py):
        value    = 123.456
        hex repr = 0x42F6E979 (big-endian canonical form)
        high     = 0x42F6,  low = 0xE979
        word-swap: registers[0] = 0xE979 (low word first),
                   registers[1] = 0x42F6 (high word second)
        Reconstruction: assemble as big-endian [0x42F6, 0xE979] → 0x42F6E979 → 123.456
    """
    _require_count(registers, 2)
    low, high = int(registers[0]) & 0xFFFF, int(registers[1]) & 0xFFFF
    # Swap back: high word first, then low word — then interpret as big-endian float
    raw = struct.pack(">HH", high, low)
    return struct.unpack(">f", raw)[0]


# ---------------------------------------------------------------------------
# Dispatcher — for use by modbus_client.py
# ---------------------------------------------------------------------------

#: Maps the byte_order string used in YAML profiles to the corresponding function.
BYTE_ORDER_HANDLERS: dict[str, "Callable[[Sequence[RegisterWord]], float]"] = {
    "big_endian":           registers_to_float32_big_endian,
    "little_endian":        registers_to_float32_little_endian,
    "big_endian_word_swap": registers_to_float32_big_endian_word_swap,
}

SUPPORTED_BYTE_ORDERS: frozenset[str] = frozenset(BYTE_ORDER_HANDLERS.keys())


def decode_registers(
    registers: Sequence[RegisterWord],
    byte_order: str,
    data_type: str = "float32",
) -> float | int:
    """Dispatch register decoding to the correct handler based on byte_order and data_type.

    Args:
        registers:  Raw 16-bit register values from pymodbus.
        byte_order: One of SUPPORTED_BYTE_ORDERS.
        data_type:  Currently supports 'float32', 'uint16', 'int16'.

    Returns:
        Decoded numeric value.

    Raises:
        ValueError: If byte_order or data_type is not supported.
    """
    if data_type == "float32":
        handler = BYTE_ORDER_HANDLERS.get(byte_order)
        if handler is None:
            raise ValueError(
                f"Unsupported byte_order '{byte_order}'. "
                f"Supported: {sorted(SUPPORTED_BYTE_ORDERS)}"
            )
        return handler(registers)

    elif data_type == "uint16":
        _require_count(registers, 1)
        return int(registers[0]) & 0xFFFF

    elif data_type == "int16":
        _require_count(registers, 1)
        raw = int(registers[0]) & 0xFFFF
        # Two's complement: values >= 0x8000 are negative
        return raw if raw < 0x8000 else raw - 0x10000

    else:
        raise ValueError(
            f"Unsupported data_type '{data_type}'. Supported: float32, uint16, int16"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _require_count(registers: Sequence[RegisterWord], expected: int) -> None:
    """Raise ValueError if registers does not have exactly `expected` elements."""
    if len(registers) != expected:
        raise ValueError(
            f"Expected {expected} register(s), got {len(registers)}. "
            "Check the profile's register_count matches the data_type."
        )


# ---------------------------------------------------------------------------
# Type hint import (not a runtime dependency)
# ---------------------------------------------------------------------------
from typing import Callable  # noqa: E402  (placed after functions for readability)
