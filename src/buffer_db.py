"""
src/buffer_db.py
-----------------
SQLite local buffering and replay-on-reconnect.

Design rules (SKILLS.md):
- Buffer-then-publish: every reading is written here FIRST before any
  publish attempt. Only marked published after confirmed PUBACK (QoS 1).
- Replay preserves original order (ORDER BY timestamp_utc) and original
  timestamps — downstream consumers need when the value was measured,
  not when it was eventually published.
- No duplicate publishes: a record is only marked published after the
  publisher explicitly calls mark_published(), not speculatively.
- No external server dependency: SQLite is file-based and ships with
  Python's standard library. This is a portability requirement.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

log = logging.getLogger(__name__)

# Default DB path — can be overridden at runtime via BufferDB(path=...)
DEFAULT_DB_PATH = Path("data") / "readings.db"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS readings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    device_name   TEXT    NOT NULL,
    timestamp_utc TEXT    NOT NULL,   -- ISO-8601 UTC, e.g. '2024-01-15T10:30:00+00:00'
    field_name    TEXT    NOT NULL,
    value         REAL    NOT NULL,
    unit          TEXT    NOT NULL DEFAULT '',
    alert         INTEGER NOT NULL DEFAULT 0,   -- 0=false, 1=true
    alert_message TEXT    NOT NULL DEFAULT '',
    published     INTEGER NOT NULL DEFAULT 0,   -- 0=unpublished, 1=published
    published_at  TEXT                           -- NULL until published
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_unpublished
    ON readings (published, timestamp_utc)
    WHERE published = 0;
"""


# ---------------------------------------------------------------------------
# BufferDB class
# ---------------------------------------------------------------------------

class BufferDB:
    """SQLite-backed reading buffer with replay-on-reconnect.

    Usage:
        db = BufferDB()                       # uses DEFAULT_DB_PATH
        db = BufferDB(path=Path("my.db"))     # custom path

        db.buffer_reading(reading)            # write before publish
        unpublished = db.get_unpublished()    # read in order
        db.mark_published(record_id)          # call only after confirmed PUBACK
    """

    def __init__(self, path: Path = DEFAULT_DB_PATH) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        log.info("BufferDB initialized at %s", self._path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def buffer_reading(self, reading: Any) -> int:
        """Write a Reading to the buffer. Call this BEFORE any publish attempt.

        Args:
            reading: A src.modbus_client.Reading instance, or any object with
                     the attributes: device_name, timestamp_utc, field_name,
                     value, unit, alert, alert_message.

        Returns:
            The row ID of the inserted record (useful for testing).
        """
        with self._conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO readings
                    (device_name, timestamp_utc, field_name, value, unit,
                     alert, alert_message, published)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    reading.device_name,
                    _to_iso(reading.timestamp_utc),
                    reading.field_name,
                    float(reading.value),
                    str(reading.unit),
                    1 if reading.alert else 0,
                    str(reading.alert_message),
                ),
            )
            row_id = cursor.lastrowid
            log.debug(
                "Buffered reading id=%d  [%s] %s = %s %s",
                row_id, reading.device_name, reading.field_name,
                reading.value, reading.unit,
            )
            return row_id

    def get_unpublished(self, limit: int = 500) -> list[dict[str, Any]]:
        """Return unpublished records in original timestamp order.

        The replay path must preserve ORDER BY timestamp_utc so downstream
        consumers see readings in the order they were actually measured.

        Args:
            limit: Maximum number of records to return per call (prevents
                   unbounded replay batches after a long outage).

        Returns:
            List of row dicts with keys: id, device_name, timestamp_utc,
            field_name, value, unit, alert, alert_message.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, device_name, timestamp_utc, field_name, value,
                       unit, alert, alert_message
                FROM   readings
                WHERE  published = 0
                ORDER BY timestamp_utc ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            {
                "id":            row[0],
                "device_name":   row[1],
                "timestamp_utc": row[2],
                "field_name":    row[3],
                "value":         row[4],
                "unit":          row[5],
                "alert":         bool(row[6]),
                "alert_message": row[7],
            }
            for row in rows
        ]

    def mark_published(self, record_id: int) -> None:
        """Mark a record as published.

        MUST be called only after receiving confirmed publish acknowledgment
        (MQTT PUBACK for QoS 1). Calling speculatively risks data loss on crash.

        Args:
            record_id: The 'id' field from a record returned by get_unpublished().
        """
        now_utc = datetime.now(tz=timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE readings SET published = 1, published_at = ? WHERE id = ?",
                (now_utc, record_id),
            )
        log.debug("Marked record id=%d as published", record_id)

    def get_alert_unpublished(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return unpublished alert records first, in timestamp order.

        Used by the edge-alert immediate-publish path (src/edge_rules.py):
        alert readings get their own priority publish queue.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, device_name, timestamp_utc, field_name, value,
                       unit, alert, alert_message
                FROM   readings
                WHERE  published = 0 AND alert = 1
                ORDER BY timestamp_utc ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            {
                "id":            row[0],
                "device_name":   row[1],
                "timestamp_utc": row[2],
                "field_name":    row[3],
                "value":         row[4],
                "unit":          row[5],
                "alert":         bool(row[6]),
                "alert_message": row[7],
            }
            for row in rows
        ]

    def count_unpublished(self) -> int:
        """Return the total number of unpublished records. Useful for monitoring."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM readings WHERE published = 0"
            ).fetchone()
        return row[0]

    def purge_published(self, keep_last_n: int = 10_000) -> int:
        """Remove old published records, keeping the most recent `keep_last_n`.

        Prevents unbounded DB growth in long-running deployments.

        Returns:
            Number of rows deleted.
        """
        with self._conn() as conn:
            result = conn.execute(
                """
                DELETE FROM readings
                WHERE published = 1
                  AND id NOT IN (
                    SELECT id FROM readings
                    WHERE published = 1
                    ORDER BY id DESC
                    LIMIT ?
                  )
                """,
                (keep_last_n,),
            )
            deleted = result.rowcount
        log.info("Purged %d published records (kept last %d)", deleted, keep_last_n)
        return deleted

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """Create table and index if they don't exist."""
        with self._conn() as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager for a SQLite connection with WAL mode enabled.

        WAL (Write-Ahead Logging) mode is safe for concurrent access and
        gives better performance for the write-heavy buffering use case.
        """
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")  # balance durability vs. speed
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_iso(ts: datetime | str) -> str:
    """Normalize a timestamp to an ISO-8601 UTC string."""
    if isinstance(ts, str):
        return ts
    if ts.tzinfo is None:
        # Assume UTC if naive — should not happen given our code, but be safe
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()
