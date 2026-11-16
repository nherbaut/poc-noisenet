"""Publish simulated environmental readings to NanoMQ."""

import json
import logging
import math
import os
import random
import signal
import threading
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

LOG = logging.getLogger("pico-client")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    host = os.getenv("MQTT_HOST", "nanomq")
    port = int(os.getenv("MQTT_PORT", "1883"))
    device_id = os.getenv("DEVICE_ID", "pico-01")
    interval = float(os.getenv("PUBLISH_INTERVAL_SECONDS", "10"))
    if not device_id or any(char in device_id for char in "/+#\x00"):
        raise ValueError("DEVICE_ID must be a nonempty MQTT topic level without wildcards")
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("PUBLISH_INTERVAL_SECONDS must be a finite positive number")
    topic = f"noisenet/{device_id}/environment/temperature"
    stopped = threading.Event()
    connected = threading.Event()
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"{device_id}-publisher",
        protocol=mqtt.MQTTv311,
    )

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            LOG.error("Connection rejected: %s", reason_code)
            return
        LOG.info("Connected to %s:%s; publishing to %s", host, port, topic)
        connected.set()

    def on_disconnect(client, userdata, flags, reason_code, properties):
        connected.clear()
        if not stopped.is_set():
            LOG.warning("Disconnected (%s); reconnecting", reason_code)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_connect_fail = lambda client, userdata: LOG.warning(
        "Cannot connect to %s:%s; retrying", host, port
    )
    client.enable_logger(LOG)
    client.reconnect_delay_set(min_delay=1, max_delay=10)
    client.max_queued_messages_set(10)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda signum, frame: stopped.set())

    client.connect_async(host, port, keepalive=30)
    client.loop_start()
    try:
        while not stopped.is_set():
            if not connected.wait(timeout=0.2):
                continue
            if stopped.is_set():
                break
            reading = {
                "device_id": device_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "temperature_c": round(random.uniform(18, 28), 2),
            }
            result = client.publish(topic, json.dumps(reading), qos=1, retain=True)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                LOG.info("Published topic=%s payload=%s", topic, json.dumps(reading))
            else:
                LOG.warning("Publish not sent immediately: %s", mqtt.error_string(result.rc))
            stopped.wait(interval)
    finally:
        client.disconnect()
        client.loop_stop()
        LOG.info("Stopped")


if __name__ == "__main__":
    main()
