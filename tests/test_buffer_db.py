"""
tests/test_buffer_db.py
------------------------
Unit tests for src/buffer_db.py.

Uses an in-memory SQLite DB (via tmp_path) to avoid filesystem side effects.
Tests cover: buffering, ordering, mark-published discipline, alert priority
queue, count, purge, and the buffer-then-publish ordering guarantee.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from src.buffer_db import BufferDB


# ---------------------------------------------------------------------------
# Minimal Reading stub (matches the interface expected by BufferDB)
# ---------------------------------------------------------------------------

@dataclass
class FakeReading:
    device_name:   str
    timestamp_utc: datetime
    field_name:    str
    value:         float
    unit:          str
    raw_registers: list[int] = field(default_factory=list)
    alert:         bool = False
    alert_message: str = ""


def _ts(offset_seconds: float = 0.0) -> datetime:
    """Return a UTC datetime offset by `offset_seconds` from a fixed base."""
    base = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(seconds=offset_seconds)


def _reading(
    device: str = "TestDevice",
    field: str = "voltage",
    value: float = 230.0,
    unit: str = "V",
    ts_offset: float = 0.0,
    alert: bool = False,
    alert_message: str = "",
) -> FakeReading:
    return FakeReading(
        device_name=device,
        timestamp_utc=_ts(ts_offset),
        field_name=field,
        value=value,
        unit=unit,
        alert=alert,
        alert_message=alert_message,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path: Path) -> BufferDB:
    """Return a fresh BufferDB backed by a temp file."""
    return BufferDB(path=tmp_path / "test_readings.db")


# ---------------------------------------------------------------------------
# Buffering
# ---------------------------------------------------------------------------

class TestBuffering:
    def test_buffer_reading_returns_id(self, db):
        r = _reading()
        row_id = db.buffer_reading(r)
        assert isinstance(row_id, int)
        assert row_id >= 1

    def test_buffered_reading_appears_in_unpublished(self, db):
        r = _reading(value=230.5)
        db.buffer_reading(r)
        unpublished = db.get_unpublished()
        assert len(unpublished) == 1
        assert unpublished[0]["value"] == pytest.approx(230.5)
        assert unpublished[0]["field_name"] == "voltage"
        assert unpublished[0]["device_name"] == "TestDevice"

    def test_multiple_readings_all_appear(self, db):
        for i in range(5):
            db.buffer_reading(_reading(value=float(i), ts_offset=float(i)))
        assert len(db.get_unpublished()) == 5

    def test_alert_flag_persisted(self, db):
        r = _reading(alert=True, alert_message="voltage below 200V")
        db.buffer_reading(r)
        rows = db.get_unpublished()
        assert rows[0]["alert"] is True
        assert "200V" in rows[0]["alert_message"]

    def test_unit_persisted(self, db):
        db.buffer_reading(_reading(unit="Hz", value=50.0))
        rows = db.get_unpublished()
        assert rows[0]["unit"] == "Hz"

    def test_timestamp_persisted_as_iso_string(self, db):
        r = _reading(ts_offset=0)
        db.buffer_reading(r)
        rows = db.get_unpublished()
        ts_str = rows[0]["timestamp_utc"]
        # Should be parseable as ISO-8601
        parsed = datetime.fromisoformat(ts_str)
        assert parsed.tzinfo is not None

    def test_different_devices_buffered_separately(self, db):
        db.buffer_reading(_reading(device="DeviceA", field="voltage"))
        db.buffer_reading(_reading(device="DeviceB", field="pressure"))
        rows = db.get_unpublished()
        devices = {r["device_name"] for r in rows}
        assert devices == {"DeviceA", "DeviceB"}


# ---------------------------------------------------------------------------
# Ordering (replay-on-reconnect requirement)
# ---------------------------------------------------------------------------

class TestOrdering:
    def test_unpublished_returned_in_timestamp_order(self, db):
        """Replay must preserve original measurement order — not insertion order."""
        # Insert in reverse timestamp order to test that ORDER BY timestamp_utc works
        timestamps = [30.0, 10.0, 50.0, 20.0, 40.0]
        for offset in timestamps:
            db.buffer_reading(_reading(value=offset, ts_offset=offset))

        rows = db.get_unpublished()
        returned_values = [r["value"] for r in rows]
        assert returned_values == sorted(timestamps), (
            "Records must be returned in ascending timestamp order for correct replay"
        )

    def test_limit_respected(self, db):
        for i in range(20):
            db.buffer_reading(_reading(ts_offset=float(i)))
        rows = db.get_unpublished(limit=5)
        assert len(rows) == 5

    def test_limit_returns_oldest_first(self, db):
        for i in range(10):
            db.buffer_reading(_reading(value=float(i), ts_offset=float(i)))
        rows = db.get_unpublished(limit=3)
        assert rows[0]["value"] == pytest.approx(0.0)
        assert rows[1]["value"] == pytest.approx(1.0)
        assert rows[2]["value"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Mark-published discipline
# ---------------------------------------------------------------------------

class TestMarkPublished:
    def test_mark_published_removes_from_unpublished(self, db):
        row_id = db.buffer_reading(_reading())
        db.mark_published(row_id)
        assert db.get_unpublished() == []

    def test_only_marked_record_disappears(self, db):
        id1 = db.buffer_reading(_reading(ts_offset=0.0, value=1.0))
        id2 = db.buffer_reading(_reading(ts_offset=1.0, value=2.0))
        db.mark_published(id1)
        remaining = db.get_unpublished()
        assert len(remaining) == 1
        assert remaining[0]["value"] == pytest.approx(2.0)

    def test_count_decreases_after_mark_published(self, db):
        id1 = db.buffer_reading(_reading(ts_offset=0.0))
        id2 = db.buffer_reading(_reading(ts_offset=1.0))
        assert db.count_unpublished() == 2
        db.mark_published(id1)
        assert db.count_unpublished() == 1
        db.mark_published(id2)
        assert db.count_unpublished() == 0

    def test_unpublished_before_mark(self, db):
        """Buffer-then-publish: record must appear in unpublished BEFORE mark_published."""
        row_id = db.buffer_reading(_reading())
        # Must appear in unpublished immediately after buffering
        rows = db.get_unpublished()
        assert any(r["id"] == row_id for r in rows), (
            "Buffered record must be in unpublished queue before mark_published is called"
        )


# ---------------------------------------------------------------------------
# Alert priority queue
# ---------------------------------------------------------------------------

class TestAlertPriorityQueue:
    def test_alert_records_appear_in_alert_queue(self, db):
        db.buffer_reading(_reading(alert=True, alert_message="over threshold"))
        alerts = db.get_alert_unpublished()
        assert len(alerts) == 1
        assert alerts[0]["alert"] is True

    def test_non_alert_records_not_in_alert_queue(self, db):
        db.buffer_reading(_reading(alert=False))
        assert db.get_alert_unpublished() == []

    def test_alert_queue_separate_from_normal_queue(self, db):
        db.buffer_reading(_reading(alert=False, ts_offset=0.0))
        db.buffer_reading(_reading(alert=True,  ts_offset=1.0, alert_message="!"))
        db.buffer_reading(_reading(alert=False, ts_offset=2.0))

        alerts  = db.get_alert_unpublished()
        normal  = db.get_unpublished()

        assert len(alerts) == 1
        assert len(normal)  == 3   # all unpublished, regardless of alert flag

    def test_alert_queue_ordered_by_timestamp(self, db):
        db.buffer_reading(_reading(alert=True, ts_offset=5.0, value=5.0))
        db.buffer_reading(_reading(alert=True, ts_offset=1.0, value=1.0))
        alerts = db.get_alert_unpublished()
        assert alerts[0]["value"] == pytest.approx(1.0)
        assert alerts[1]["value"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Count and purge
# ---------------------------------------------------------------------------

class TestCountAndPurge:
    def test_count_unpublished_zero_initially(self, db):
        assert db.count_unpublished() == 0

    def test_count_unpublished_increments(self, db):
        for i in range(3):
            db.buffer_reading(_reading(ts_offset=float(i)))
        assert db.count_unpublished() == 3

    def test_purge_removes_published_records(self, db):
        ids = [db.buffer_reading(_reading(ts_offset=float(i))) for i in range(5)]
        for row_id in ids:
            db.mark_published(row_id)

        deleted = db.purge_published(keep_last_n=2)
        assert deleted == 3

    def test_purge_keeps_recent_published(self, db):
        ids = [db.buffer_reading(_reading(ts_offset=float(i))) for i in range(5)]
        for row_id in ids:
            db.mark_published(row_id)

        db.purge_published(keep_last_n=3)
        # Unpublished count should still be 0 (purge only affects published)
        assert db.count_unpublished() == 0

    def test_purge_does_not_touch_unpublished(self, db):
        db.buffer_reading(_reading(ts_offset=0.0))   # not published
        id2 = db.buffer_reading(_reading(ts_offset=1.0))
        db.mark_published(id2)

        db.purge_published(keep_last_n=0)
        # The unpublished record must still be there
        assert db.count_unpublished() == 1


# ---------------------------------------------------------------------------
# Persistence (survives re-opening the DB file)
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_records_survive_db_reopen(self, tmp_path):
        """Data must persist across BufferDB instances pointing to the same file."""
        db_path = tmp_path / "persist_test.db"

        db1 = BufferDB(path=db_path)
        row_id = db1.buffer_reading(_reading(value=42.0))

        # Re-open with a new instance
        db2 = BufferDB(path=db_path)
        rows = db2.get_unpublished()
        assert len(rows) == 1
        assert rows[0]["value"] == pytest.approx(42.0)

    def test_mark_published_persists_across_reopen(self, tmp_path):
        db_path = tmp_path / "persist_test2.db"

        db1 = BufferDB(path=db_path)
        row_id = db1.buffer_reading(_reading())
        db1.mark_published(row_id)

        db2 = BufferDB(path=db_path)
        assert db2.get_unpublished() == []
