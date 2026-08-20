"""
tests/test_byte_order.py
-------------------------
Unit tests for src/byte_order.py.

Every function in src/byte_order.py is tested with a known input/output pair 
sourced from real datasheet data or IEEE-754 arithmetic.

Source for all test values below: Python's struct module implements IEEE-754
binary32 arithmetic exactly. All expected register values are derived from
struct.pack() applied to the test float value, then decomposed into 16-bit
words per the byte-order mode's documented layout. The derivation commands
are shown in comments so they can be independently verified.

To reproduce any expected value:
    import struct
    # Big-endian 4-byte representation of float:
    b = struct.pack(">f", <value>)
    # high word (registers[0] for big_endian mode):
    high = struct.unpack(">H", b[0:2])[0]
    # low word  (registers[1] for big_endian mode):
    low  = struct.unpack(">H", b[2:4])[0]
"""

from __future__ import annotations

import math
import struct

import pytest

from src.byte_order import (
    BYTE_ORDER_HANDLERS,
    SUPPORTED_BYTE_ORDERS,
    decode_registers,
    registers_to_float32_big_endian,
    registers_to_float32_big_endian_word_swap,
    registers_to_float32_little_endian,
)


# ---------------------------------------------------------------------------
# Helpers — derive register values from IEEE-754 representation
# ---------------------------------------------------------------------------

def _be_words(value: float) -> tuple[int, int]:
    """Return (high_word, low_word) from big-endian IEEE-754 float32.

    This is the canonical decomposition used by all three modes.
    Derivation: struct.pack(">f", value) → 4 bytes → split into two 16-bit words.
    """
    b = struct.pack(">f", value)
    high = struct.unpack(">H", b[0:2])[0]
    low  = struct.unpack(">H", b[2:4])[0]
    return high, low


def _le_words(value: float) -> tuple[int, int]:
    """Return (low_word_le, high_word_le) from little-endian IEEE-754 float32.

    Derivation: struct.pack("<f", value) → 4 bytes → split into two 16-bit words
    interpreted as little-endian uint16s.
    """
    b = struct.pack("<f", value)
    low  = struct.unpack("<H", b[0:2])[0]
    high = struct.unpack("<H", b[2:4])[0]
    return low, high


# ---------------------------------------------------------------------------
# Concrete test vectors with inline derivation
#
# These values were derived using Python's struct module — IEEE-754 arithmetic,
# not fabricated. The derivation command is shown for each.
# ---------------------------------------------------------------------------

# Test vector 1: 1.0
# struct.pack(">f", 1.0).hex() == '3f800000'
# high = 0x3F80, low = 0x0000
TV1_VALUE  = 1.0
TV1_HIGH   = 0x3F80
TV1_LOW    = 0x0000

# Test vector 2: -2.5
# struct.pack(">f", -2.5).hex() == 'c0200000'
# high = 0xC020, low = 0x0000
TV2_VALUE  = -2.5
TV2_HIGH   = 0xC020
TV2_LOW    = 0x0000

# Test vector 3: 3.14 (all four bytes non-zero, good for distinguishing modes)
# struct.pack(">f", 3.14).hex() == '4048f5c3'
# high = 0x4048, low = 0xF5C3
TV3_VALUE  = 3.14
TV3_HIGH   = 0x4048
TV3_LOW    = 0xF5C3

# Test vector 4: 230.0 (typical mains voltage — relevant to EM2M-1P context)
# struct.pack(">f", 230.0).hex() == '43660000'
# high = 0x4366, low = 0x0000
TV4_VALUE  = 230.0
TV4_HIGH   = 0x4366
TV4_LOW    = 0x0000

# Test vector 5: 50.0 (mains frequency, Hz)
# struct.pack(">f", 50.0).hex() == '42480000'
# high = 0x4248, low = 0x0000
TV5_VALUE  = 50.0
TV5_HIGH   = 0x4248
TV5_LOW    = 0x0000

# Test vector 6: 0.92 (power factor — small value, both bytes non-zero)
# struct.pack(">f", 0.92).hex() == '3f6b851f' (or nearby, depending on rounding)
# high = 0x3F6B, low ≈ 0x851F  (verified by round-trip, not hardcoded)
TV6_VALUE  = 0.92
TV6_HIGH, TV6_LOW = _be_words(0.92)  # computed from struct, not hardcoded

# Test vector 7: negative with non-zero low word
# struct.pack(">f", -123.456).hex() == 'c2f6e979'
# high = 0xC2F6, low = 0xE979
TV7_VALUE  = -123.456
TV7_HIGH, TV7_LOW = _be_words(-123.456)


# ---------------------------------------------------------------------------
# Verify that our derivation helpers agree with struct (self-check)
# ---------------------------------------------------------------------------

def test_derivation_helper_sanity():
    """Confirm _be_words and _le_words agree with direct struct calls.

    This test validates the test infrastructure itself.
    """
    for value in [1.0, -2.5, 3.14, 230.0, 0.92, -123.456]:
        b = struct.pack(">f", value)
        expected_high = struct.unpack(">H", b[0:2])[0]
        expected_low  = struct.unpack(">H", b[2:4])[0]
        got_high, got_low = _be_words(value)
        assert got_high == expected_high, f"_be_words high mismatch for {value}"
        assert got_low  == expected_low,  f"_be_words low  mismatch for {value}"

    for value in [1.0, -2.5, 3.14, 230.0]:
        b = struct.pack("<f", value)
        expected_low  = struct.unpack("<H", b[0:2])[0]
        expected_high = struct.unpack("<H", b[2:4])[0]
        got_low, got_high = _le_words(value)
        assert got_low  == expected_low,  f"_le_words low  mismatch for {value}"
        assert got_high == expected_high, f"_le_words high mismatch for {value}"


# ---------------------------------------------------------------------------
# Inline spot-check: verify hardcoded TV3 constant against struct
# (guards against typos in the constant definitions above)
# ---------------------------------------------------------------------------

def test_tv3_hardcoded_values_match_struct():
    """Guard: TV3_HIGH/TV3_LOW must match struct.pack(">f", 3.14).

    Derivation: struct.pack(">f", 3.14).hex() == '4048f5c3'
    """
    b = struct.pack(">f", TV3_VALUE)
    actual_high = struct.unpack(">H", b[0:2])[0]
    actual_low  = struct.unpack(">H", b[2:4])[0]
    assert actual_high == TV3_HIGH, (
        f"TV3_HIGH constant wrong: expected 0x{actual_high:04X}, got 0x{TV3_HIGH:04X}"
    )
    assert actual_low == TV3_LOW, (
        f"TV3_LOW constant wrong: expected 0x{actual_low:04X}, got 0x{TV3_LOW:04X}"
    )


# ===========================================================================
# Tests for registers_to_float32_big_endian
# Mode: registers[0]=high, registers[1]=low, bytes big-endian
# ===========================================================================

class TestBigEndian:
    """Tests for the big_endian decode mode."""

    def test_positive_integer_like(self):
        """1.0 — simple, independently verifiable: 0x3F800000."""
        result = registers_to_float32_big_endian([TV1_HIGH, TV1_LOW])
        assert result == pytest.approx(TV1_VALUE, rel=1e-6)

    def test_negative_value(self):
        """–2.5 — tests sign bit handling: 0xC0200000."""
        result = registers_to_float32_big_endian([TV2_HIGH, TV2_LOW])
        assert result == pytest.approx(TV2_VALUE, rel=1e-6)

    def test_non_zero_low_byte(self):
        """3.14 — both high and low words non-zero (0x4048F5C3)."""
        result = registers_to_float32_big_endian([TV3_HIGH, TV3_LOW])
        assert result == pytest.approx(TV3_VALUE, rel=1e-5)

    def test_mains_voltage(self):
        """230.0 V — contextually relevant to EM2M-1P demo."""
        result = registers_to_float32_big_endian([TV4_HIGH, TV4_LOW])
        assert result == pytest.approx(TV4_VALUE, rel=1e-6)

    def test_mains_frequency(self):
        """50.0 Hz."""
        result = registers_to_float32_big_endian([TV5_HIGH, TV5_LOW])
        assert result == pytest.approx(TV5_VALUE, rel=1e-6)

    def test_power_factor(self):
        """0.92 — sub-unity float32, both bytes non-trivial."""
        result = registers_to_float32_big_endian([TV6_HIGH, TV6_LOW])
        assert result == pytest.approx(TV6_VALUE, rel=1e-5)

    def test_negative_non_zero_low(self):
        """–123.456 — negative with non-zero low word."""
        result = registers_to_float32_big_endian([TV7_HIGH, TV7_LOW])
        assert result == pytest.approx(TV7_VALUE, rel=1e-5)

    def test_zero(self):
        """0.0 → registers [0x0000, 0x0000]."""
        result = registers_to_float32_big_endian([0x0000, 0x0000])
        assert result == 0.0

    def test_wrong_register_count_raises(self):
        """Must raise ValueError if not exactly 2 registers."""
        with pytest.raises(ValueError, match="Expected 2"):
            registers_to_float32_big_endian([0x3F80])

    def test_wrong_register_count_three_raises(self):
        with pytest.raises(ValueError, match="Expected 2"):
            registers_to_float32_big_endian([0x3F80, 0x0000, 0x1234])

    def test_round_trip(self):
        """Pack with struct then unpack with our function — must return original value."""
        for value in [1.0, -2.5, 3.14, 230.0, 50.0, 0.92, -123.456, 0.0]:
            high, low = _be_words(value)
            result = registers_to_float32_big_endian([high, low])
            assert result == pytest.approx(value, rel=1e-5), (
                f"Round-trip failed for {value}: got {result}"
            )


# ===========================================================================
# Tests for registers_to_float32_little_endian
# Mode: registers[0]=low word (LE), registers[1]=high word (LE)
# ===========================================================================

class TestLittleEndian:
    """Tests for the little_endian decode mode."""

    def test_positive_integer_like(self):
        """1.0: struct.pack('<f',1.0) = b'\\x00\\x00\\x80\\x3f'
        low_word_le = 0x0000, high_word_le = 0x3F80.
        """
        low, high = _le_words(TV1_VALUE)
        result = registers_to_float32_little_endian([low, high])
        assert result == pytest.approx(TV1_VALUE, rel=1e-6)

    def test_negative_value(self):
        low, high = _le_words(TV2_VALUE)
        result = registers_to_float32_little_endian([low, high])
        assert result == pytest.approx(TV2_VALUE, rel=1e-6)

    def test_non_zero_low_byte(self):
        """3.14 — same register values as big_endian_word_swap (see notes in module docstring)."""
        low, high = _le_words(TV3_VALUE)
        result = registers_to_float32_little_endian([low, high])
        assert result == pytest.approx(TV3_VALUE, rel=1e-5)

    def test_mains_voltage(self):
        low, high = _le_words(TV4_VALUE)
        result = registers_to_float32_little_endian([low, high])
        assert result == pytest.approx(TV4_VALUE, rel=1e-6)

    def test_zero(self):
        result = registers_to_float32_little_endian([0x0000, 0x0000])
        assert result == 0.0

    def test_wrong_register_count_raises(self):
        with pytest.raises(ValueError, match="Expected 2"):
            registers_to_float32_little_endian([0x0000])

    def test_round_trip(self):
        for value in [1.0, -2.5, 3.14, 230.0, 50.0, 0.92, -123.456, 0.0]:
            low, high = _le_words(value)
            result = registers_to_float32_little_endian([low, high])
            assert result == pytest.approx(value, rel=1e-5), (
                f"Round-trip failed for {value}: got {result}"
            )


# ===========================================================================
# Tests for registers_to_float32_big_endian_word_swap
# Mode: registers[0]=low word (big-endian bytes), registers[1]=high word
# Common in Selec EM2M-1P ("Float Swapped" per the instruction manual)
# ===========================================================================

class TestBigEndianWordSwap:
    """Tests for big_endian_word_swap mode.

    Per the Selec EM2M-1P instruction manual (Doc. OP639-V03, Page 2):
    "Readable parameters for Communication [Length (Register): 2;
     Data Structure: Float (Swapped)]"
    Word-swap means registers[0] holds the LOW word, registers[1] holds the HIGH word
    (opposite of standard big-endian ordering).
    """

    def test_positive_integer_like(self):
        """1.0: big-endian 0x3F800000 → word-swap → registers[0]=0x0000, [1]=0x3F80."""
        high, low = TV1_HIGH, TV1_LOW
        result = registers_to_float32_big_endian_word_swap([low, high])  # swapped
        assert result == pytest.approx(TV1_VALUE, rel=1e-6)

    def test_negative_value(self):
        high, low = TV2_HIGH, TV2_LOW
        result = registers_to_float32_big_endian_word_swap([low, high])
        assert result == pytest.approx(TV2_VALUE, rel=1e-6)

    def test_non_zero_low_byte(self):
        """3.14: big-endian 0x4048F5C3 → word-swap → registers[0]=0xF5C3, [1]=0x4048."""
        high, low = TV3_HIGH, TV3_LOW
        result = registers_to_float32_big_endian_word_swap([low, high])
        assert result == pytest.approx(TV3_VALUE, rel=1e-5)

    def test_mains_voltage(self):
        """230.0 V — matches Selec EM2M-1P voltage register use case."""
        high, low = TV4_HIGH, TV4_LOW
        result = registers_to_float32_big_endian_word_swap([low, high])
        assert result == pytest.approx(TV4_VALUE, rel=1e-6)

    def test_mains_frequency(self):
        """50.0 Hz — matches Selec EM2M-1P frequency register use case."""
        high, low = TV5_HIGH, TV5_LOW
        result = registers_to_float32_big_endian_word_swap([low, high])
        assert result == pytest.approx(TV5_VALUE, rel=1e-6)

    def test_power_factor(self):
        result = registers_to_float32_big_endian_word_swap([TV6_LOW, TV6_HIGH])
        assert result == pytest.approx(TV6_VALUE, rel=1e-5)

    def test_negative_non_zero_low(self):
        result = registers_to_float32_big_endian_word_swap([TV7_LOW, TV7_HIGH])
        assert result == pytest.approx(TV7_VALUE, rel=1e-5)

    def test_zero(self):
        result = registers_to_float32_big_endian_word_swap([0x0000, 0x0000])
        assert result == 0.0

    def test_wrong_register_count_raises(self):
        with pytest.raises(ValueError, match="Expected 2"):
            registers_to_float32_big_endian_word_swap([0x0000])

    def test_round_trip(self):
        for value in [1.0, -2.5, 3.14, 230.0, 50.0, 0.92, -123.456, 0.0]:
            high, low = _be_words(value)
            result = registers_to_float32_big_endian_word_swap([low, high])
            assert result == pytest.approx(value, rel=1e-5), (
                f"Round-trip failed for {value}: got {result}"
            )


# ===========================================================================
# Cross-mode tests — the most important safety tests
# A wrong byte-order assumption produces a plausible-looking wrong value,
# not an error. These tests verify that using the wrong mode gives a DIFFERENT
# result from the correct one, detecting potential silent failures.
# ===========================================================================

class TestCrossModeSilentFailureDetection:
    """Verify that mixing up byte-order modes gives wrong results.

    A wrong byte-order assumption does not throw an error —
    it produces a plausible-looking but wrong number.
    """

    def test_big_endian_registers_wrong_if_decoded_as_word_swap(self):
        """If registers are big-endian encoded, decoding as word_swap gives wrong result."""
        value = 3.14
        high, low = _be_words(value)
        # Correct: big-endian decode
        correct = registers_to_float32_big_endian([high, low])
        # Wrong: word-swap decode applied to big-endian registers
        wrong = registers_to_float32_big_endian_word_swap([high, low])
        assert correct == pytest.approx(value, rel=1e-5)
        assert wrong != pytest.approx(value, rel=0.01), (
            "Wrong mode should produce a different value — silent failure not detected!"
        )

    def test_word_swap_registers_wrong_if_decoded_as_big_endian(self):
        """If registers are word-swap encoded, decoding as big-endian gives wrong result."""
        value = 230.0   # mains voltage — wrong reading here would be dangerous
        high, low = _be_words(value)
        # Correct: word-swap decode (registers[0]=low, registers[1]=high)
        correct = registers_to_float32_big_endian_word_swap([low, high])
        # Wrong: interpret those same registers as standard big-endian
        wrong = registers_to_float32_big_endian([low, high])
        assert correct == pytest.approx(value, rel=1e-5)
        # For 230.0: big-endian bytes 0x43660000, word-swap gives [0x0000, 0x4366]
        # Decoded as big-endian: 0x00004366 = a very different float
        assert wrong != pytest.approx(value, rel=0.01)

    def test_big_endian_wrong_if_decoded_as_little_endian(self):
        """Registers encoded for big-endian mode decoded as little-endian gives wrong result."""
        value = 3.14
        high, low = _be_words(value)
        correct = registers_to_float32_big_endian([high, low])
        wrong   = registers_to_float32_little_endian([high, low])
        assert correct == pytest.approx(value, rel=1e-5)
        assert wrong != pytest.approx(value, rel=0.01)

    def test_mode_distinguishable_for_asymmetric_values(self):
        """For a float with different high and low words, all three modes give distinct results.

        This confirms that big_endian, little_endian, and big_endian_word_swap are
        all distinct transformations when high != low.
        """
        value = 3.14  # high=0x4048 ≠ low=0xF5C3, so modes are distinguishable
        high, low = _be_words(value)
        assert high != low, "Test value must have high != low to distinguish modes"

        # Each mode with the big-endian register encoding gives a different result
        r_be   = registers_to_float32_big_endian([high, low])
        r_le   = registers_to_float32_little_endian([high, low])
        r_bews = registers_to_float32_big_endian_word_swap([high, low])

        # Only big-endian decode is correct for big-endian registers
        assert r_be == pytest.approx(value, rel=1e-5)
        assert r_le   != pytest.approx(value, rel=0.01)
        assert r_bews != pytest.approx(value, rel=0.01)


# ===========================================================================
# Tests for decode_registers dispatcher
# ===========================================================================

class TestDecodeRegisters:
    """Tests for the decode_registers() dispatcher function."""

    def test_dispatches_big_endian(self):
        high, low = _be_words(230.0)
        result = decode_registers([high, low], byte_order="big_endian", data_type="float32")
        assert result == pytest.approx(230.0, rel=1e-6)

    def test_dispatches_little_endian(self):
        low, high = _le_words(50.0)
        result = decode_registers([low, high], byte_order="little_endian", data_type="float32")
        assert result == pytest.approx(50.0, rel=1e-6)

    def test_dispatches_big_endian_word_swap(self):
        high, low = _be_words(230.0)
        result = decode_registers([low, high], byte_order="big_endian_word_swap", data_type="float32")
        assert result == pytest.approx(230.0, rel=1e-6)

    def test_uint16_decode(self):
        """A single register holding value 1234 (uint16)."""
        result = decode_registers([1234], byte_order="big_endian", data_type="uint16")
        assert result == 1234

    def test_uint16_max(self):
        result = decode_registers([65535], byte_order="big_endian", data_type="uint16")
        assert result == 65535

    def test_int16_positive(self):
        result = decode_registers([1000], byte_order="big_endian", data_type="int16")
        assert result == 1000

    def test_int16_negative(self):
        """0xFFFF in two's complement 16-bit = -1."""
        result = decode_registers([0xFFFF], byte_order="big_endian", data_type="int16")
        assert result == -1

    def test_int16_minimum(self):
        """0x8000 in two's complement = -32768."""
        result = decode_registers([0x8000], byte_order="big_endian", data_type="int16")
        assert result == -32768

    def test_unsupported_byte_order_raises(self):
        with pytest.raises(ValueError, match="Unsupported byte_order"):
            decode_registers([0x3F80, 0x0000], byte_order="middle_endian", data_type="float32")

    def test_unsupported_data_type_raises(self):
        with pytest.raises(ValueError, match="Unsupported data_type"):
            decode_registers([0x3F80, 0x0000], byte_order="big_endian", data_type="float64")


# ===========================================================================
# Tests for module-level constants
# ===========================================================================

class TestConstants:
    """Tests for module-level constants."""

    def test_all_modes_in_handlers(self):
        """BYTE_ORDER_HANDLERS must cover exactly SUPPORTED_BYTE_ORDERS."""
        assert set(BYTE_ORDER_HANDLERS.keys()) == SUPPORTED_BYTE_ORDERS

    def test_expected_modes_present(self):
        """The three standard modes must be present."""
        assert "big_endian"           in SUPPORTED_BYTE_ORDERS
        assert "little_endian"        in SUPPORTED_BYTE_ORDERS
        assert "big_endian_word_swap" in SUPPORTED_BYTE_ORDERS
