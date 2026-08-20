"""
src/main.py
-----------
Orchestrates the full polling → buffer → alert-check → publish loop.

Key design rules enforced here (AGENTS.md / SKILLS.md):
- Each device's poll+publish cycle is wrapped in its own try/except.
  One failing device MUST NOT stop polling for the others.
- Failure of one device is logged with device name and timestamp.
- Readings are buffered BEFORE publish; only marked published on confirmed PUBACK.
- Alert readings get an immediate publish path, not the normal batch interval.
- device-specific logic never appears here — it lives in YAML profiles.

Usage:
    python src/main.py --config-dir config/devices/ [options]
    python src/main.py --help
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path so direct script execution (python src/main.py) resolves 'src' modules
_root_dir = Path(__file__).resolve().parent.parent
if str(_root_dir) not in sys.path:
    sys.path.insert(0, str(_root_dir))

# ---------------------------------------------------------------------------
# Logging setup — do this before importing project modules so their loggers
# inherit the root configuration.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("main")

from src.profile_loader import load_all_profiles
from src.modbus_client   import poll_device
from src.buffer_db       import BufferDB
from src.edge_rules      import evaluate_readings, build_alert_summary
from src.publisher_mqtt  import MQTTPublisher


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Modbus-to-Cloud data layer — polling and publishing loop.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Simulator mode (start simulators first in separate terminals):
  python src/main.py --config-dir config/devices/ --gateway-host 127.0.0.1 --gateway-port 5020

  # Real gateway:
  python src/main.py --config-dir config/devices/ --gateway-host 192.168.1.100

  # No MQTT (log only):
  python src/main.py --config-dir config/devices/ --no-publish
""",
    )
    parser.add_argument(
        "--config-dir", default="config/devices/",
        help="Directory containing *.yaml device profile files (default: config/devices/)",
    )
    parser.add_argument(
        "--gateway-host", default=None,
        help="Override gateway IP/hostname for all devices. If not set, uses "
             "each profile's 'simulator.host' or falls back to 127.0.0.1",
    )
    parser.add_argument(
        "--gateway-port", type=int, default=None,
        help="Override TCP port for all devices. If not set, uses each profile's "
             "'simulator.port' or falls back to 5020",
    )
    parser.add_argument(
        "--mqtt-host", default="broker.hivemq.com",
        help="MQTT broker hostname (default: broker.hivemq.com — DEMO ONLY)",
    )
    parser.add_argument(
        "--mqtt-port", type=int, default=1883,
        help="MQTT broker port (default: 1883)",
    )
    parser.add_argument(
        "--db-path", default="data/readings.db",
        help="Path to the SQLite buffer database (default: data/readings.db)",
    )
    parser.add_argument(
        "--no-publish", action="store_true",
        help="Disable MQTT publishing (log readings to stdout only)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity level (default: INFO)",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Device state tracker
# ---------------------------------------------------------------------------

class _DeviceState:
    """Tracks per-device polling timing so each respects its own interval."""

    def __init__(self, profile: dict[str, Any]) -> None:
        self.profile         = profile
        self.device_name     = profile["device_name"]
        self.poll_interval   = float(profile["poll_interval_seconds"])
        self.last_poll_time  = 0.0   # monotonic timestamp; 0 means "never polled"

    def is_due(self) -> bool:
        return time.monotonic() - self.last_poll_time >= self.poll_interval

    def mark_polled(self) -> None:
        self.last_poll_time = time.monotonic()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    """Main entry point: load profiles, then run the polling loop indefinitely."""

    # Adjust log level
    logging.getLogger().setLevel(args.log_level.upper())

    config_dir = Path(args.config_dir)
    log.info("Loading profiles from %s", config_dir)

    profiles = load_all_profiles(config_dir)
    if not profiles:
        log.error("No valid device profiles found in %s — exiting.", config_dir)
        sys.exit(1)

    log.info("Loaded %d device profile(s): %s",
             len(profiles), [p["device_name"] for p in profiles])

    # Initialize buffer DB
    db = BufferDB(path=Path(args.db_path))
    log.info("Buffer DB: %s", args.db_path)

    # Initialize MQTT publisher (optional)
    publisher: MQTTPublisher | None = None
    if not args.no_publish:
        publisher = MQTTPublisher(host=args.mqtt_host, port=args.mqtt_port)
        try:
            publisher.connect()
        except Exception as exc:
            log.warning(
                "Could not connect to MQTT broker: %s — running in buffer-only mode", exc
            )
            publisher = None

    # Build per-device state objects
    device_states = [_DeviceState(p) for p in profiles]

    log.info("Starting polling loop. Press Ctrl-C to stop.")
    try:
        _main_loop(device_states, db, publisher, args)
    except KeyboardInterrupt:
        log.info("Interrupted by user — shutting down.")
    finally:
        if publisher:
            publisher.disconnect()


def _main_loop(
    device_states: list[_DeviceState],
    db: BufferDB,
    publisher: MQTTPublisher | None,
    args: argparse.Namespace,
) -> None:
    """Inner polling loop. Runs until interrupted."""

    while True:
        any_polled = False

        for state in device_states:
            if not state.is_due():
                continue

            any_polled = True
            state.mark_polled()
            _poll_one_device(state, db, publisher, args)

        # Replay unpublished records (normal + alert) if MQTT is up
        replaying_count = 0
        if publisher and publisher.is_connected():
            replaying_count = db.count_unpublished()
            _replay_unpublished(db, publisher)

        # Publish gateway sync status so the dashboard can show buffering state.
        # This runs every iteration; QoS 0 so it never blocks the poll loop.
        if publisher:
            buffered = db.count_unpublished()
            if not publisher.is_connected():
                sync_state = "buffering"
            elif replaying_count > 0:
                sync_state = "replaying"
            else:
                sync_state = "live"
            publisher.publish_status({
                "state":           sync_state,
                "buffered_count":  buffered,
                "replaying_count": replaying_count,
                "timestamp_utc":   datetime.now(tz=timezone.utc).isoformat(),
                "devices_online":  [s.device_name for s in device_states],
            })

        if not any_polled:
            time.sleep(0.1)   # short sleep to avoid busy-wait



def _poll_one_device(
    state: "_DeviceState",
    db: BufferDB,
    publisher: MQTTPublisher | None,
    args: argparse.Namespace,
) -> None:
    """Poll one device, buffer readings, and dispatch alerts.

    Per AGENTS.md: all exceptions are caught here so one device's failure
    does not stop polling for other devices. The failure is logged with
    device name and timestamp.
    """
    profile     = state.profile
    device_name = state.device_name
    now_label   = datetime.now(tz=timezone.utc).isoformat()

    # Resolve host/port: CLI override > profile simulator block > fallback
    sim_cfg  = profile.get("simulator", {})
    host = args.gateway_host or sim_cfg.get("host", "127.0.0.1")
    port = args.gateway_port or sim_cfg.get("port", 5020)

    try:
        readings = poll_device(host=host, port=port, profile=profile)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "[%s] @ %s  Poll failed: %s", device_name, now_label, exc
        )
        return

    if not readings:
        log.warning("[%s] Poll returned no readings", device_name)
        return

    # Evaluate edge rules — splits into (normal, alerts)
    normal_readings, alert_readings = evaluate_readings(readings, profile)

    # Buffer ALL readings first (buffer-then-publish ordering)
    all_readings = normal_readings + alert_readings
    record_ids: dict[int, Any] = {}   # reading object → db row id

    for reading in all_readings:
        row_id = db.buffer_reading(reading)
        record_ids[id(reading)] = row_id
        # Print labeled JSON to stdout (Phase 3 acceptance criterion)
        print(json.dumps(reading.to_dict(), indent=None, default=str), flush=True)

    # Immediate publish path for alerts — don't wait for the batch interval
    if alert_readings and publisher and publisher.is_connected():
        log.info(
            "[%s] Dispatching %d alert reading(s) immediately",
            device_name, len(alert_readings),
        )
        if alert_readings:
            log.info(build_alert_summary(alert_readings))
        for reading in alert_readings:
            record = reading.to_dict()
            success = publisher.publish_alert(record)
            if success:
                db.mark_published(record_ids[id(reading)])


def _replay_unpublished(db: BufferDB, publisher: MQTTPublisher) -> None:
    """Publish buffered (unpublished) records in original timestamp order.

    Called each loop iteration when MQTT is connected. This is what provides
    replay-on-reconnect after a network outage.
    """
    unpublished = db.get_unpublished(limit=100)
    if not unpublished:
        return

    log.info("Replaying %d unpublished record(s)...", len(unpublished))

    for record in unpublished:
        if not publisher.is_connected():
            log.warning("MQTT disconnected during replay — pausing")
            break

        success = publisher.publish_record(record)
        if success:
            db.mark_published(record["id"])
        else:
            log.warning(
                "Replay publish failed for record id=%d [%s] %s — will retry next cycle",
                record["id"], record["device_name"], record["field_name"],
            )
            break  # Don't skip ahead — preserve order


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()
    run(args)


if __name__ == "__main__":
    main()
