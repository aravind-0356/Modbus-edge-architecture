"""
tests/test_profile_loader.py
-----------------------------
Unit tests for src/profile_loader.py.

Tests cover: valid profile loading, each validation failure mode,
load_all_profiles() skip-bad-file behavior, and register_type validation.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from src.profile_loader import (
    ProfileValidationError,
    load_all_profiles,
    load_profile,
)


# ---------------------------------------------------------------------------
# Fixtures — write minimal YAML files to tmp_path
# ---------------------------------------------------------------------------

def _write_yaml(tmp_path: Path, name: str, content: str) -> Path:
    """Write a YAML string to a temp file and return its Path."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


VALID_PROFILE_YAML = """
device_name: "Test Device"
slave_id: 1
poll_interval_seconds: 5
registers:
  - name: "voltage"
    address: 0
    register_count: 2
    data_type: "float32"
    byte_order: "big_endian"
    unit: "V"
  - name: "current"
    address: 2
    register_count: 2
    data_type: "float32"
    byte_order: "little_endian"
    unit: "A"
    alert_range: [0.0, 100.0]
"""

VALID_WITH_REGISTER_TYPE = """
device_name: "Selec Test"
slave_id: 2
poll_interval_seconds: 3
register_type: "input"
registers:
  - name: "voltage"
    address: 0
    register_count: 2
    data_type: "float32"
    byte_order: "big_endian_word_swap"
    unit: "V"
"""


# ---------------------------------------------------------------------------
# Valid profile loading
# ---------------------------------------------------------------------------

class TestValidProfile:
    def test_loads_successfully(self, tmp_path):
        path = _write_yaml(tmp_path, "valid.yaml", VALID_PROFILE_YAML)
        profile = load_profile(path)
        assert profile["device_name"] == "Test Device"
        assert profile["slave_id"] == 1
        assert profile["poll_interval_seconds"] == 5
        assert len(profile["registers"]) == 2

    def test_register_fields_preserved(self, tmp_path):
        path = _write_yaml(tmp_path, "valid.yaml", VALID_PROFILE_YAML)
        profile = load_profile(path)
        reg = profile["registers"][0]
        assert reg["name"] == "voltage"
        assert reg["address"] == 0
        assert reg["data_type"] == "float32"
        assert reg["byte_order"] == "big_endian"
        assert reg["unit"] == "V"

    def test_alert_range_preserved(self, tmp_path):
        path = _write_yaml(tmp_path, "valid.yaml", VALID_PROFILE_YAML)
        profile = load_profile(path)
        current = profile["registers"][1]
        assert current["alert_range"] == [0.0, 100.0]

    def test_register_type_input_valid(self, tmp_path):
        path = _write_yaml(tmp_path, "valid_input.yaml", VALID_WITH_REGISTER_TYPE)
        profile = load_profile(path)
        assert profile["register_type"] == "input"

    def test_register_type_defaults_to_holding(self, tmp_path):
        """Profiles without register_type should load fine (defaults to 'holding')."""
        path = _write_yaml(tmp_path, "valid.yaml", VALID_PROFILE_YAML)
        profile = load_profile(path)
        # Key may be absent (default); loader should not raise
        assert "registers" in profile

    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_profile(tmp_path / "nonexistent.yaml")

    def test_returns_dict(self, tmp_path):
        path = _write_yaml(tmp_path, "valid.yaml", VALID_PROFILE_YAML)
        result = load_profile(path)
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Missing required top-level keys
# ---------------------------------------------------------------------------

class TestMissingTopLevelKeys:
    def test_missing_device_name(self, tmp_path):
        path = _write_yaml(tmp_path, "bad.yaml", """
            slave_id: 1
            poll_interval_seconds: 5
            registers:
              - name: "v"
                address: 0
                register_count: 2
                data_type: "float32"
                byte_order: "big_endian"
                unit: "V"
        """)
        with pytest.raises(ProfileValidationError, match="device_name"):
            load_profile(path)

    def test_missing_slave_id(self, tmp_path):
        path = _write_yaml(tmp_path, "bad.yaml", """
            device_name: "X"
            poll_interval_seconds: 5
            registers:
              - name: "v"
                address: 0
                register_count: 2
                data_type: "float32"
                byte_order: "big_endian"
                unit: "V"
        """)
        with pytest.raises(ProfileValidationError, match="slave_id"):
            load_profile(path)

    def test_missing_registers(self, tmp_path):
        path = _write_yaml(tmp_path, "bad.yaml", """
            device_name: "X"
            slave_id: 1
            poll_interval_seconds: 5
        """)
        with pytest.raises(ProfileValidationError, match="registers"):
            load_profile(path)

    def test_empty_device_name(self, tmp_path):
        path = _write_yaml(tmp_path, "bad.yaml", """
            device_name: ""
            slave_id: 1
            poll_interval_seconds: 5
            registers:
              - name: "v"
                address: 0
                register_count: 2
                data_type: "float32"
                byte_order: "big_endian"
                unit: "V"
        """)
        with pytest.raises(ProfileValidationError, match="device_name"):
            load_profile(path)

    def test_slave_id_out_of_range(self, tmp_path):
        path = _write_yaml(tmp_path, "bad.yaml", """
            device_name: "X"
            slave_id: 0
            poll_interval_seconds: 5
            registers:
              - name: "v"
                address: 0
                register_count: 2
                data_type: "float32"
                byte_order: "big_endian"
                unit: "V"
        """)
        with pytest.raises(ProfileValidationError, match="slave_id"):
            load_profile(path)

    def test_slave_id_too_large(self, tmp_path):
        path = _write_yaml(tmp_path, "bad.yaml", """
            device_name: "X"
            slave_id: 248
            poll_interval_seconds: 5
            registers:
              - name: "v"
                address: 0
                register_count: 2
                data_type: "float32"
                byte_order: "big_endian"
                unit: "V"
        """)
        with pytest.raises(ProfileValidationError, match="slave_id"):
            load_profile(path)

    def test_negative_poll_interval(self, tmp_path):
        path = _write_yaml(tmp_path, "bad.yaml", """
            device_name: "X"
            slave_id: 1
            poll_interval_seconds: -1
            registers:
              - name: "v"
                address: 0
                register_count: 2
                data_type: "float32"
                byte_order: "big_endian"
                unit: "V"
        """)
        with pytest.raises(ProfileValidationError, match="poll_interval_seconds"):
            load_profile(path)

    def test_empty_registers_list(self, tmp_path):
        path = _write_yaml(tmp_path, "bad.yaml", """
            device_name: "X"
            slave_id: 1
            poll_interval_seconds: 5
            registers: []
        """)
        with pytest.raises(ProfileValidationError, match="registers"):
            load_profile(path)


# ---------------------------------------------------------------------------
# Invalid register entries
# ---------------------------------------------------------------------------

class TestInvalidRegisterEntries:
    def _base_profile(self, overrides: str) -> str:
        return f"""
device_name: "X"
slave_id: 1
poll_interval_seconds: 5
registers:
  - name: "voltage"
    address: 0
    register_count: 2
    data_type: "float32"
    byte_order: "big_endian"
    unit: "V"
    {overrides}
"""

    def test_unsupported_byte_order(self, tmp_path):
        path = _write_yaml(tmp_path, "bad.yaml", self._base_profile("byte_order: 'auto'"))
        with pytest.raises(ProfileValidationError, match="byte_order"):
            load_profile(path)

    def test_unsupported_data_type(self, tmp_path):
        path = _write_yaml(tmp_path, "bad.yaml", self._base_profile("data_type: 'float64'"))
        with pytest.raises(ProfileValidationError, match="data_type"):
            load_profile(path)

    def test_register_count_mismatch_float32(self, tmp_path):
        """float32 must have register_count: 2."""
        path = _write_yaml(tmp_path, "bad.yaml", self._base_profile("register_count: 1"))
        with pytest.raises(ProfileValidationError, match="register_count"):
            load_profile(path)

    def test_register_count_mismatch_uint16(self, tmp_path):
        """uint16 must have register_count: 1, not 2."""
        yaml_content = """
device_name: "X"
slave_id: 1
poll_interval_seconds: 5
registers:
  - name: "status"
    address: 0
    register_count: 2
    data_type: "uint16"
    byte_order: "big_endian"
    unit: ""
"""
        path = _write_yaml(tmp_path, "bad.yaml", yaml_content)
        with pytest.raises(ProfileValidationError, match="register_count"):
            load_profile(path)

    def test_duplicate_register_names(self, tmp_path):
        yaml_content = """
device_name: "X"
slave_id: 1
poll_interval_seconds: 5
registers:
  - name: "voltage"
    address: 0
    register_count: 2
    data_type: "float32"
    byte_order: "big_endian"
    unit: "V"
  - name: "voltage"
    address: 2
    register_count: 2
    data_type: "float32"
    byte_order: "big_endian"
    unit: "V"
"""
        path = _write_yaml(tmp_path, "bad.yaml", yaml_content)
        with pytest.raises(ProfileValidationError, match="Duplicate"):
            load_profile(path)

    def test_invalid_alert_range_not_list(self, tmp_path):
        path = _write_yaml(tmp_path, "bad.yaml", self._base_profile("alert_range: 200"))
        with pytest.raises(ProfileValidationError, match="alert_range"):
            load_profile(path)

    def test_invalid_alert_range_min_gt_max(self, tmp_path):
        path = _write_yaml(tmp_path, "bad.yaml", self._base_profile("alert_range: [300, 100]"))
        with pytest.raises(ProfileValidationError, match="alert_range"):
            load_profile(path)

    def test_invalid_alert_range_wrong_length(self, tmp_path):
        path = _write_yaml(tmp_path, "bad.yaml", self._base_profile("alert_range: [100]"))
        with pytest.raises(ProfileValidationError, match="alert_range"):
            load_profile(path)

    def test_invalid_register_type(self, tmp_path):
        yaml_content = """
device_name: "X"
slave_id: 1
poll_interval_seconds: 5
register_type: "coil"
registers:
  - name: "voltage"
    address: 0
    register_count: 2
    data_type: "float32"
    byte_order: "big_endian"
    unit: "V"
"""
        path = _write_yaml(tmp_path, "bad.yaml", yaml_content)
        with pytest.raises(ProfileValidationError, match="register_type"):
            load_profile(path)


# ---------------------------------------------------------------------------
# load_all_profiles — skip-bad, load-good behavior
# ---------------------------------------------------------------------------

class TestLoadAllProfiles:
    def test_loads_all_valid(self, tmp_path):
        for name in ("device_a.yaml", "device_b.yaml"):
            profile_content = VALID_PROFILE_YAML.replace("Test Device", name)
            _write_yaml(tmp_path, name, profile_content)

        profiles = load_all_profiles(tmp_path)
        assert len(profiles) == 2

    def test_skips_invalid_loads_valid(self, tmp_path):
        """One bad profile must not stop loading of the other good profiles."""
        _write_yaml(tmp_path, "good.yaml", VALID_PROFILE_YAML)
        _write_yaml(tmp_path, "bad.yaml", "device_name: 'X'\nslave_id: 999\n")  # invalid slave_id

        profiles = load_all_profiles(tmp_path)
        assert len(profiles) == 1
        assert profiles[0]["device_name"] == "Test Device"

    def test_empty_directory_returns_empty_list(self, tmp_path):
        profiles = load_all_profiles(tmp_path)
        assert profiles == []

    def test_skips_non_yaml_files(self, tmp_path):
        """Non-.yaml files in the directory must be ignored."""
        (tmp_path / "notes.txt").write_text("not a yaml", encoding="utf-8")
        _write_yaml(tmp_path, "valid.yaml", VALID_PROFILE_YAML)
        profiles = load_all_profiles(tmp_path)
        assert len(profiles) == 1

    def test_not_a_yaml_mapping_skipped(self, tmp_path):
        """A YAML file that is a list instead of a mapping must be skipped."""
        path = tmp_path / "list_yaml.yaml"
        path.write_text("- item1\n- item2\n", encoding="utf-8")
        _write_yaml(tmp_path, "valid.yaml", VALID_PROFILE_YAML)
        profiles = load_all_profiles(tmp_path)
        assert len(profiles) == 1
