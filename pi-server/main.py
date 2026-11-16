"""Subscribe to environmental readings and log valid temperature data."""

import json
import logging
import math
import os
import signal
import threading
from datetime import datetime

import paho.mqtt.client as mqtt

LOG = logging.getLogger("pi-server")


def on_message(client, userdata, message):
    try:
        reading = json.loads(message.payload.decode("utf-8"))
        if not isinstance(reading, dict):
            raise ValueError("expected a JSON object")
        device = reading["device_id"]
        timestamp = reading["timestamp"]
        temperature = reading["temperature_c"]
        if not isinstance(device, str) or not device:
            raise ValueError("device_id must be a nonempty string")
        if not isinstance(timestamp, str) or datetime.fromisoformat(timestamp).utcoffset() is None:
            raise ValueError("timestamp must be ISO-8601 with a timezone")
        if type(temperature) not in (int, float) or not math.isfinite(temperature):
            raise ValueError("temperature_c must be a finite number")
    except (ValueError, KeyError, TypeError, UnicodeError, OverflowError) as exc:
        LOG.warning("Ignoring malformed reading on %s: %s", message.topic, exc)
        return
    LOG.info(
        "Reading topic=%s timestamp=%s device=%s temperature_c=%.2f retained=%s",
        message.topic, timestamp, device, temperature, message.retain,
    )


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    host = os.getenv("MQTT_HOST", "nanomq")
    port = int(os.getenv("MQTT_PORT", "1883"))
    topic_filter = os.getenv("MQTT_TOPIC_FILTER", "noisenet/+/environment/temperature")
    stopped = threading.Event()
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        protocol=mqtt.MQTTv311,
    )

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            LOG.error("Connection rejected: %s", reason_code)
            return
        result, _ = client.subscribe(topic_filter, qos=1)
        if result != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"Subscription failed: {mqtt.error_string(result)}")
        LOG.info("Connected to %s:%s; subscribing to %s", host, port, topic_filter)

    def on_disconnect(client, userdata, flags, reason_code, properties):
        if not stopped.is_set():
            LOG.warning("Disconnected (%s); reconnecting", reason_code)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.on_connect_fail = lambda client, userdata: LOG.warning(
        "Cannot connect to %s:%s; retrying", host, port
    )
    client.enable_logger(LOG)
    client.reconnect_delay_set(min_delay=1, max_delay=10)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda signum, frame: stopped.set())

    client.connect_async(host, port, keepalive=30)
    client.loop_start()
    try:
        stopped.wait()
    finally:
        client.disconnect()
        client.loop_stop()
        LOG.info("Stopped")


if __name__ == "__main__":
    main()
