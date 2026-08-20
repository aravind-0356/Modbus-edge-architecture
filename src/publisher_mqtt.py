"""
src/publisher_mqtt.py
----------------------
MQTT publisher for normalized readings.

Design Requirements:
- QoS 1 (at-least-once): required because replay logic depends on knowing
  whether a publish actually succeeded. QoS 0 (fire-and-forget) is not
  sufficient here.
- Topic structure: gateway/<device_name>/<field_name>  — hierarchical,
  lets downstream consumers subscribe selectively.
- Mark published only AFTER confirmed PUBACK, not after calling publish().
- Thread-safe: uses paho-mqtt's blocking publish API.
- Public broker (HiveMQ) is for DEMO USE ONLY — not private, not
  authenticated. Never publish real client-identifying data here.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# paho-mqtt import — handle v1.x and v2.x APIs
# ---------------------------------------------------------------------------
try:
    import paho.mqtt.client as mqtt
    # paho-mqtt 2.x introduced CallbackAPIVersion
    try:
        _MQTT_CLIENT = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        _PAHO_VERSION = 2
    except AttributeError:
        _PAHO_VERSION = 1
except ImportError as exc:
    raise ImportError(
        "paho-mqtt is required. Install it with: pip install paho-mqtt"
    ) from exc

# Default MQTT broker for demo use — public, not private
DEMO_BROKER_HOST = "broker.hivemq.com"
DEMO_BROKER_PORT = 1883

# Topic template — keep this pattern consistent with node-red-flow.json.
# The 'opendaq_rohit' prefix makes topics unique on the shared public HiveMQ
# broker so other users' messages don't bleed into our Node-RED subscription.
TOPIC_PREFIX   = "opendaq_rohit"
TOPIC_TEMPLATE = "{prefix}/gateway/{device_name}/{field_name}"


# ---------------------------------------------------------------------------
# MQTTPublisher class
# ---------------------------------------------------------------------------

class MQTTPublisher:
    """Publishes Reading records to an MQTT broker using QoS 1.

    Lifecycle:
        publisher = MQTTPublisher(host="broker.hivemq.com", port=1883)
        publisher.connect()
        success = publisher.publish_record(record_dict)
        publisher.disconnect()

    Or use as a context manager:
        with MQTTPublisher(...) as pub:
            pub.publish_record(record)
    """

    def __init__(
        self,
        host: str = DEMO_BROKER_HOST,
        port: int = DEMO_BROKER_PORT,
        client_id: str = "modbus_cloud_gateway",
        username: str | None = None,
        password: str | None = None,
        keepalive: int = 60,
        publish_timeout: float = 10.0,
    ) -> None:
        self._host = host
        self._port = port
        self._keepalive = keepalive
        self._publish_timeout = publish_timeout
        self._connected = False

        # Build paho client — compatible with v1 and v2
        if _PAHO_VERSION >= 2:
            self._client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
                clean_session=True,
            )
        else:
            self._client = mqtt.Client(  # type: ignore[call-overload]
                client_id=client_id,
                clean_session=True,
            )

        if username is not None:
            self._client.username_pw_set(username, password)

        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Connect to the MQTT broker and start the network loop."""
        log.info("Connecting to MQTT broker %s:%d", self._host, self._port)
        self._client.connect(self._host, self._port, keepalive=self._keepalive)
        self._client.loop_start()
        # Give the broker a moment to accept the connection
        deadline = time.monotonic() + 5.0
        while not self._connected and time.monotonic() < deadline:
            time.sleep(0.05)
        if not self._connected:
            raise ConnectionError(
                f"Could not connect to MQTT broker {self._host}:{self._port} within 5 seconds"
            )
        log.info("MQTT connected to %s:%d", self._host, self._port)

    def disconnect(self) -> None:
        """Disconnect cleanly."""
        self._client.loop_stop()
        self._client.disconnect()
        self._connected = False
        log.info("MQTT disconnected")

    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_record(self, record: dict[str, Any]) -> bool:
        """Publish one reading record to MQTT at QoS 1.

        The topic is built from device_name and field_name using TOPIC_TEMPLATE.
        The payload is a JSON object with the full record.

        IMPORTANT: This function blocks until PUBACK is received (or timeout).
        Only returns True after the broker has acknowledged the message.
        The caller must NOT mark the record as published unless this returns True.

        Args:
            record: A dict with keys: device_name, field_name, timestamp_utc,
                    value, unit, alert, alert_message (as from Reading.to_dict()).

        Returns:
            True if publish was acknowledged (PUBACK received).
            False if publish failed or timed out.
        """
        if not self._connected:
            log.warning("MQTT publish attempted while disconnected — skipping")
            return False

        device_name = record.get("device_name", "unknown")
        field_name  = record.get("field_name",  "unknown")
        topic = TOPIC_TEMPLATE.format(
            prefix=TOPIC_PREFIX,
            device_name=device_name,
            field_name=field_name,
        )
        payload = json.dumps(record, default=str)

        try:
            info = self._client.publish(topic, payload, qos=1, retain=False)
            # Wait for PUBACK — this is what makes QoS 1 meaningful
            info.wait_for_publish(timeout=self._publish_timeout)

            if info.is_published():
                log.debug("Published to %s (mid=%d)", topic, info.mid)
                return True
            else:
                log.warning(
                    "Publish not acknowledged within %.1fs for topic %s",
                    self._publish_timeout, topic,
                )
                return False

        except Exception as exc:  # noqa: BLE001
            log.error("MQTT publish error for %s: %s", topic, exc)
            return False

    def publish_alert(self, record: dict[str, Any]) -> bool:
        """Publish an alert reading on a dedicated alert sub-topic.

        Alert topic: gateway/<device_name>/alerts/<field_name>
        This gives dashboard and downstream consumers a separate subscription
        point for alert conditions without flooding the normal data topics.

        Returns:
            True if publish was acknowledged.
        """
        device_name = record.get("device_name", "unknown")
        field_name  = record.get("field_name",  "unknown")
        alert_topic = f"{TOPIC_PREFIX}/gateway/{device_name}/alerts/{field_name}"
        payload = json.dumps(record, default=str)

        try:
            info = self._client.publish(alert_topic, payload, qos=1, retain=False)
            info.wait_for_publish(timeout=self._publish_timeout)
            if info.is_published():
                log.info("Alert published to %s", alert_topic)
                return True
            return False
        except Exception as exc:  # noqa: BLE001
            log.error("MQTT alert publish error for %s: %s", alert_topic, exc)
            return False

    def publish_status(self, status: dict[str, Any]) -> None:
        """Publish gateway sync status to opendaq_rohit/gateway/_status.

        This is the hook that lets the dashboard show buffering/replay state.
        QoS 0 is intentional here: status is ephemeral, losing one heartbeat
        is harmless, and we don't want status publishes to block the poll loop.

        Args:
            status: Dict with keys: state ('live'|'buffering'|'replaying'),
                    buffered_count (int), replaying_count (int),
                    timestamp_utc (str), devices_online (list[str]).
        """
        if not self._connected:
            return
        topic   = f"{TOPIC_PREFIX}/gateway/_status"
        payload = json.dumps(status, default=str)
        try:
            self._client.publish(topic, payload, qos=0, retain=False)
            log.debug("Status published: state=%s buffered=%d",
                      status.get("state"), status.get("buffered_count", 0))
        except Exception as exc:  # noqa: BLE001
            log.debug("Status publish error (non-fatal): %s", exc)


    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "MQTTPublisher":
        self.connect()
        return self

    def __exit__(self, *_: Any) -> None:
        self.disconnect()

    # ------------------------------------------------------------------
    # paho callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client: Any, userdata: Any, flags: Any, *args: Any) -> None:
        # paho v2 passes reason_code as args[0]; v1 passes rc
        rc = args[0] if args else 0
        rc_value = rc if isinstance(rc, int) else rc.value
        if rc_value == 0:
            self._connected = True
            log.debug("MQTT on_connect: success")
        else:
            log.error("MQTT on_connect: failed with rc=%s", rc)

    def _on_disconnect(self, client: Any, userdata: Any, *args: Any) -> None:
        self._connected = False
        log.warning("MQTT disconnected (args=%s)", args)
