"""NoiseNet Wi-Fi/MQTT prototype for Raspberry Pi Pico 2 W and DFRobot SEN0232.

Reads the SEN0232 once per publication and publishes a JSON reading to a
local MQTT broker over plain TCP (no TLS), QoS 0, retain disabled.

With WIFI_SSID = None (or without secrets.py), the application runs in
sensor-only mode: readings are printed on the USB console, no network is used
and umqtt.simple is not required.

Configuration is read from ``secrets.py`` (copy ``config.example.py``).
Passwords are never printed.

This is a bench prototype. It does not replace the NoiseNet LoRaWAN firmware.
"""

import json
import time

from sen0232 import SEN0232, LevelAggregator, VALID_ADC_GPIOS
import wioe5

APP_NAME = "noisenet-pico2w-mqtt"
APP_VERSION = (1, 0)  # reported in the LoRaWAN boot message; bump on behaviour changes

DEFAULTS = {
    "WIFI_SSID": None,
    "WIFI_PASSWORD": None,
    "WIFI_COUNTRY": None,
    "MQTT_BROKER": None,
    "MQTT_PORT": 1883,
    "MQTT_USER": None,
    "MQTT_PASSWORD": None,
    "MQTT_KEEPALIVE_S": 60,
    "MQTT_TOPIC": None,
    "DEVICE_ID": None,
    "ADC_GPIO": 26,
    "ADC_REFERENCE_V": 3.3,
    "CAL_A": 1.0,
    "CAL_B": 0.0,
    "PUBLISH_INTERVAL_S": 1.0,
    "SAMPLE_RATE_HZ": 1.0,
    "CONNECT_TIMEOUT_S": 15,
    "SOCKET_TIMEOUT_S": 5,
    "RETRY_DELAY_S": 5,
    "LORA_ENABLED": False,
    "LORA_UART_ID": 0,
    "LORA_TX_GPIO": 0,
    "LORA_RX_GPIO": 1,
    "LORA_BAUD": 9600,
    "LORA_SLOT_S": 60,
    "LORA_SLOTS_PER_UPLINK": 5,
    "LORA_INTERVAL_S": None,  # replaced by LORA_SLOT_S / LORA_SLOTS_PER_UPLINK
    "LORA_PORT": 8,
    "LORA_BOOT_PORT": 9,
    "LORA_BOOT_MESSAGE": True,
    "LORA_DATA_RATE": 5,
    "LORA_ADR": False,
    "LORA_JOIN_RETRY_S": 120,
    "LORA_APP_KEY": None,
    "LORA_JOIN_EUI": None,
}

SECRET_KEYS = ("WIFI_PASSWORD", "MQTT_PASSWORD", "LORA_APP_KEY")

# Upper bound for one idle sleep, so MQTT input (PINGRESP) is drained
# regularly and the loop stays responsive.
MAX_IDLE_SLEEP_MS = 200
MIN_IDLE_SLEEP_MS = 1
MAX_SAMPLE_RATE_HZ = 200

WIFI_STATUS_TEXT = {
    -1: "connection failed",
    -2: "access point not found",
    -3: "authentication failed (check WIFI_PASSWORD)",
}


def log(msg):
    print(msg)


def describe_error(exc):
    return "%s: %s" % (type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config(module):
    """Merge a user configuration module over DEFAULTS."""
    cfg = dict(DEFAULTS)
    for key in DEFAULTS:
        if hasattr(module, key):
            cfg[key] = getattr(module, key)
    return cfg


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_placeholder(value):
    return isinstance(value, str) and value.startswith("<") and value.endswith(">")


def _check_text(errors, cfg, key, required, allow_empty=False):
    value = cfg[key]
    if value is None:
        if required:
            errors.append("%s is required" % key)
        return
    if not isinstance(value, str):
        errors.append("%s must be a string" % key)
    elif _is_placeholder(value):
        errors.append("%s still contains the placeholder from config.example.py" % key)
    elif value == "" and not allow_empty:
        errors.append("%s must not be empty" % key)


def _check_number(errors, cfg, key, minimum, maximum, integer=False):
    value = cfg[key]
    if not _is_number(value) or (integer and not isinstance(value, int)):
        errors.append("%s must be %s" % (key, "an integer" if integer else "a number"))
    elif value < minimum or value > maximum:
        errors.append("%s must be between %s and %s" % (key, minimum, maximum))


def _check_topic_part(errors, key, value, allow_slash):
    if value is None:
        return
    if not isinstance(value, str) or value == "":
        errors.append("%s must be a non-empty string" % key)
        return
    for ch in "+#":
        if ch in value:
            errors.append("%s must not contain MQTT wildcard '%s'" % (key, ch))
    if not allow_slash and "/" in value:
        errors.append("%s must not contain '/'" % key)


def wifi_enabled(cfg):
    """Wi-Fi and MQTT are used only when WIFI_SSID is set (not None)."""
    return cfg["WIFI_SSID"] is not None


def _is_hex(value, length):
    if not isinstance(value, str) or len(value) != length:
        return False
    for ch in value:
        if ch not in "0123456789abcdefABCDEF":
            return False
    return True


def _validate_lora(errors, cfg):
    uart_id = cfg["LORA_UART_ID"]
    if uart_id not in (0, 1) or isinstance(uart_id, bool):
        errors.append("LORA_UART_ID must be 0 or 1")
    else:
        if cfg["LORA_TX_GPIO"] not in wioe5.UART_TX_PINS[uart_id]:
            errors.append("LORA_TX_GPIO must be one of %s for UART%d"
                          % (wioe5.UART_TX_PINS[uart_id], uart_id))
        if cfg["LORA_RX_GPIO"] not in wioe5.UART_RX_PINS[uart_id]:
            errors.append("LORA_RX_GPIO must be one of %s for UART%d"
                          % (wioe5.UART_RX_PINS[uart_id], uart_id))
    if cfg["ADC_GPIO"] in (cfg["LORA_TX_GPIO"], cfg["LORA_RX_GPIO"]):
        errors.append("ADC_GPIO must differ from the LoRa UART pins")
    _check_number(errors, cfg, "LORA_BAUD", 1200, 230400, integer=True)
    if cfg["LORA_INTERVAL_S"] is not None:
        errors.append("LORA_INTERVAL_S is replaced by LORA_SLOT_S (seconds per maximum) "
                      "and LORA_SLOTS_PER_UPLINK (maxima per uplink)")
    slot = cfg["LORA_SLOT_S"]
    if not isinstance(slot, int) or isinstance(slot, bool) or not 10 <= slot <= 2550 \
            or slot % 10:
        errors.append("LORA_SLOT_S must be a multiple of 10 between 10 and 2550")
    _check_number(errors, cfg, "LORA_SLOTS_PER_UPLINK", 1, wioe5.MAX_SLOTS, integer=True)
    _check_number(errors, cfg, "LORA_PORT", 1, 223, integer=True)
    _check_number(errors, cfg, "LORA_BOOT_PORT", 1, 223, integer=True)
    if cfg["LORA_BOOT_PORT"] == cfg["LORA_PORT"]:
        errors.append("LORA_BOOT_PORT must differ from LORA_PORT")
    if not isinstance(cfg["LORA_BOOT_MESSAGE"], bool):
        errors.append("LORA_BOOT_MESSAGE must be True or False")
    _check_number(errors, cfg, "LORA_JOIN_RETRY_S", 10, 86400)
    if cfg["LORA_DATA_RATE"] is not None:
        _check_number(errors, cfg, "LORA_DATA_RATE", 0, 5, integer=True)
    if not isinstance(cfg["LORA_ADR"], bool):
        errors.append("LORA_ADR must be True or False")
    if cfg["LORA_APP_KEY"] is not None and not _is_hex(cfg["LORA_APP_KEY"], 32):
        errors.append("LORA_APP_KEY must be 32 hexadecimal digits, or None")
    if cfg["LORA_JOIN_EUI"] is not None and not _is_hex(cfg["LORA_JOIN_EUI"], 16):
        errors.append("LORA_JOIN_EUI must be 16 hexadecimal digits, or None")
    rate = cfg["SAMPLE_RATE_HZ"]
    if _is_number(rate) and _is_number(slot) and rate * slot < 1:
        errors.append("SAMPLE_RATE_HZ * LORA_SLOT_S must be >= 1")


def validate_config(cfg):
    """Return a list of human-readable errors. Secret values are never echoed.

    With WIFI_SSID = None (sensor-only mode), Wi-Fi and MQTT settings are
    ignored and not validated.
    """
    errors = []
    _check_number(errors, cfg, "PUBLISH_INTERVAL_S", 0.1, 86400)
    _check_number(errors, cfg, "SAMPLE_RATE_HZ", 0.01, MAX_SAMPLE_RATE_HZ)
    rate = cfg["SAMPLE_RATE_HZ"]
    interval = cfg["PUBLISH_INTERVAL_S"]
    if _is_number(rate) and _is_number(interval) and rate * interval < 0.999:
        errors.append(
            "SAMPLE_RATE_HZ * PUBLISH_INTERVAL_S must be >= 1 "
            "(at least one sample per reading)"
        )
    if cfg["ADC_GPIO"] not in VALID_ADC_GPIOS or isinstance(cfg["ADC_GPIO"], bool):
        errors.append("ADC_GPIO must be one of %s" % (VALID_ADC_GPIOS,))
    _check_number(errors, cfg, "ADC_REFERENCE_V", 1.0, 3.6)
    _check_number(errors, cfg, "CAL_A", 0.1, 10.0)
    _check_number(errors, cfg, "CAL_B", -100.0, 100.0)
    if cfg["LORA_ENABLED"] is not False:
        if cfg["LORA_ENABLED"] is not True:
            errors.append("LORA_ENABLED must be True or False")
        else:
            _validate_lora(errors, cfg)
    if not wifi_enabled(cfg):
        return errors

    _check_text(errors, cfg, "WIFI_SSID", required=True)
    _check_text(errors, cfg, "WIFI_PASSWORD", required=True, allow_empty=True)
    _check_text(errors, cfg, "MQTT_BROKER", required=True)
    _check_text(errors, cfg, "MQTT_USER", required=False)
    _check_text(errors, cfg, "MQTT_PASSWORD", required=False, allow_empty=True)
    if cfg["WIFI_COUNTRY"] is not None:
        country = cfg["WIFI_COUNTRY"]
        if not isinstance(country, str) or len(country) != 2:
            errors.append("WIFI_COUNTRY must be a two-letter code such as 'FR', or None")

    broker = cfg["MQTT_BROKER"]
    if isinstance(broker, str) and broker.lower() in ("localhost", "127.0.0.1", "::1"):
        errors.append(
            "MQTT_BROKER must be the broker's address on the Wi-Fi network; "
            "'localhost' refers to the Pico itself"
        )
    if cfg["MQTT_PASSWORD"] is not None and cfg["MQTT_USER"] is None:
        errors.append("MQTT_PASSWORD is set but MQTT_USER is not")

    _check_number(errors, cfg, "MQTT_PORT", 1, 65535, integer=True)
    _check_number(errors, cfg, "MQTT_KEEPALIVE_S", 0, 65535, integer=True)
    _check_topic_part(errors, "DEVICE_ID", cfg["DEVICE_ID"], allow_slash=False)
    _check_topic_part(errors, "MQTT_TOPIC", cfg["MQTT_TOPIC"], allow_slash=True)

    _check_number(errors, cfg, "CONNECT_TIMEOUT_S", 1, 300)
    _check_number(errors, cfg, "SOCKET_TIMEOUT_S", 1, 300)
    _check_number(errors, cfg, "RETRY_DELAY_S", 1, 3600)

    keepalive = cfg["MQTT_KEEPALIVE_S"]
    timeout = cfg["SOCKET_TIMEOUT_S"]
    if _is_number(keepalive) and _is_number(timeout) and 0 < keepalive <= 2 * timeout:
        errors.append("MQTT_KEEPALIVE_S must be 0 or greater than 2 * SOCKET_TIMEOUT_S")
    return errors


def redact(cfg):
    """Copy of cfg with secret values replaced by 'set'/'not set'."""
    shown = dict(cfg)
    for key in SECRET_KEYS:
        shown[key] = "<set>" if cfg.get(key) else "<not set>"
    return shown


def describe_config(cfg, device_id, topic):
    shown = redact(cfg)
    if wifi_enabled(cfg):
        lines = [
            "Wi-Fi SSID: %s (password %s)" % (shown["WIFI_SSID"], shown["WIFI_PASSWORD"]),
            "MQTT broker: %s:%s (plain TCP, no TLS)"
            % (shown["MQTT_BROKER"], shown["MQTT_PORT"]),
            "MQTT user: %s (password %s)"
            % (shown["MQTT_USER"] or "<none>", shown["MQTT_PASSWORD"]),
            "Device ID: %s" % device_id,
            "Topic: %s" % topic,
        ]
    else:
        lines = [
            "Wi-Fi: disabled (WIFI_SSID = None); sensor-only mode, "
            "readings are shown on the USB console only",
        ]
    if cfg["LORA_ENABLED"]:
        dr = cfg["LORA_DATA_RATE"]
        slots = cfg["LORA_SLOTS_PER_UPLINK"]
        uplink_s = cfg["LORA_SLOT_S"] * slots
        lines.append(
            "LoRaWAN: Wio-E5 on UART%d (TX GP%d, RX GP%d), max level per %s s slot, "
            "%d slots per uplink (every %d s, %d bytes), port %d, AppKey %s"
            % (cfg["LORA_UART_ID"], cfg["LORA_TX_GPIO"], cfg["LORA_RX_GPIO"],
               cfg["LORA_SLOT_S"], slots, uplink_s, wioe5.payload_length(slots),
               cfg["LORA_PORT"], shown["LORA_APP_KEY"])
        )
        lines.append("LoRaWAN boot message: %s" % (
            "port %d, once after the join" % cfg["LORA_BOOT_PORT"]
            if cfg["LORA_BOOT_MESSAGE"] else "disabled"))
        if dr is not None and not cfg["LORA_ADR"]:
            daily = wioe5.daily_airtime_s(dr, uplink_s, wioe5.payload_length(slots))
            note = ""
            if daily > wioe5.TTN_FAIR_USE_S_PER_DAY:
                note = " (exceeds the TTN Fair Use Policy of %d s/day)" % (
                    wioe5.TTN_FAIR_USE_S_PER_DAY)
            lines.append("LoRaWAN airtime: about %.0f s/day at DR%d%s" % (daily, dr, note))
    else:
        lines.append("LoRaWAN: disabled (LORA_ENABLED = False)")
    return lines + [
        "ADC: GP%d, reference %.3f V" % (shown["ADC_GPIO"], shown["ADC_REFERENCE_V"]),
        "Calibration: a=%s b=%s%s"
        % (
            shown["CAL_A"],
            shown["CAL_B"],
            " (default: no assembled-system calibration)"
            if shown["CAL_A"] == 1.0 and shown["CAL_B"] == 0.0
            else "",
        ),
        "Reading interval: %s s" % shown["PUBLISH_INTERVAL_S"],
        "Sampling: %s Hz (about %d samples per reading, energy-averaged)"
        % (shown["SAMPLE_RATE_HZ"],
           int(shown["SAMPLE_RATE_HZ"] * shown["PUBLISH_INTERVAL_S"] + 0.5)),
    ]


def default_device_id():
    import binascii
    import machine

    return "pico2w-" + binascii.hexlify(machine.unique_id()).decode()


def default_topic(device_id):
    return "noisenet/%s/sound" % device_id


# ---------------------------------------------------------------------------
# Monotonic clock (handles ticks_ms() wraparound)
# ---------------------------------------------------------------------------


class Clock:
    """Accumulates ticks_diff() steps into a non-wrapping millisecond count.

    MicroPython ticks wrap; ticks_diff() is valid for intervals shorter than
    half the tick period (days on rp2). now_ms() must be called at least that
    often, which the main loop guarantees.
    """

    def __init__(self, ticks_ms=None, ticks_diff=None, sleep_ms=None):
        self._ticks_ms = ticks_ms if ticks_ms is not None else time.ticks_ms
        self._ticks_diff = ticks_diff if ticks_diff is not None else time.ticks_diff
        self._sleep_ms = sleep_ms if sleep_ms is not None else time.sleep_ms
        self._last = self._ticks_ms()
        self._elapsed = 0

    def now_ms(self):
        current = self._ticks_ms()
        step = self._ticks_diff(current, self._last)
        if step > 0:
            self._elapsed += step
        self._last = current
        return self._elapsed

    def sleep_ms(self, ms):
        if ms > 0:
            self._sleep_ms(int(ms))
        self.now_ms()


# ---------------------------------------------------------------------------
# Wi-Fi
# ---------------------------------------------------------------------------


class WiFi:
    POLL_MS = 250

    def __init__(self, cfg, clock, wlan=None, logger=log):
        self.ssid = cfg["WIFI_SSID"]
        self._password = cfg["WIFI_PASSWORD"]
        self.timeout_ms = int(cfg["CONNECT_TIMEOUT_S"] * 1000)
        self.clock = clock
        self.log = logger
        if wlan is None:
            import network

            country = cfg["WIFI_COUNTRY"]
            if country and hasattr(network, "country"):
                network.country(country.upper())
            wlan = network.WLAN(network.STA_IF)
        self.wlan = wlan

    def is_connected(self):
        try:
            return bool(self.wlan.isconnected())
        except OSError:
            return False

    def _address(self):
        try:
            return self.wlan.ifconfig()[0]
        except Exception:
            return "?"

    def _drop(self):
        try:
            self.wlan.disconnect()
        except Exception:
            pass

    def connect(self):
        """Bounded connection attempt. Returns True when an IP is assigned."""
        self.wlan.active(True)
        if self.is_connected():
            return True
        self.log("Wi-Fi: connecting to %r (timeout %d s)" % (self.ssid, self.timeout_ms // 1000))
        self._drop()
        if self._password:
            self.wlan.connect(self.ssid, self._password)
        else:
            self.wlan.connect(self.ssid)
        deadline = self.clock.now_ms() + self.timeout_ms
        reason = "timeout"
        while self.clock.now_ms() < deadline:
            if self.is_connected():
                self.log("Wi-Fi: connected, address %s" % self._address())
                return True
            status = self.wlan.status()
            if isinstance(status, int) and status < 0:
                reason = WIFI_STATUS_TEXT.get(status, "status %d" % status)
                break
            self.clock.sleep_ms(self.POLL_MS)
        self.log("Wi-Fi: failed (%s)" % reason)
        self._drop()
        return False


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------


def load_mqtt_client_class():
    try:
        from umqtt.simple import MQTTClient
    except ImportError:
        return None
    return MQTTClient


def _ignore_message(topic, msg):
    pass


class MqttLink:
    """Owns at most one umqtt.simple client and its socket."""

    def __init__(self, cfg, client_class, client_id, clock, logger=log):
        self.client_class = client_class
        self.client_id = client_id
        self.server = cfg["MQTT_BROKER"]
        self.port = cfg["MQTT_PORT"]
        self.user = cfg["MQTT_USER"]
        self._password = cfg["MQTT_PASSWORD"]
        self.keepalive_s = cfg["MQTT_KEEPALIVE_S"]
        self.socket_timeout_s = cfg["SOCKET_TIMEOUT_S"]
        self.clock = clock
        self.log = logger
        self.client = None
        self.last_tx_ms = 0
        self._connect_timeout_supported = True

    @property
    def connected(self):
        return self.client is not None

    def _apply_socket_timeout(self, client):
        sock = getattr(client, "sock", None)
        if sock is not None:
            sock.settimeout(self.socket_timeout_s)

    def connect(self):
        self.close()
        password = self._password
        if self.user is not None and password is None:
            password = ""
        client = self.client_class(
            self.client_id,
            self.server,
            port=self.port,
            user=self.user,
            password=password,
            keepalive=self.keepalive_s,
        )
        try:
            if self._connect_timeout_supported:
                try:
                    client.connect(timeout=self.socket_timeout_s)
                except TypeError:
                    # Releases of umqtt.simple before connect(timeout=...)
                    self._connect_timeout_supported = False
                    self.log(
                        "MQTT: installed umqtt.simple has no connect timeout; "
                        "update it for bounded connection attempts"
                    )
                    self._close_client(client)
                    client.connect()
            else:
                client.connect()
            self._apply_socket_timeout(client)
            client.set_callback(_ignore_message)
        except BaseException:
            self._close_client(client)
            raise
        self.client = client
        self.last_tx_ms = self.clock.now_ms()

    def publish(self, topic, payload):
        self.client.publish(topic, payload, False, 0)  # retain=False, qos=0
        self.last_tx_ms = self.clock.now_ms()

    def ms_until_ping(self, now_ms):
        if not self.keepalive_s:
            return MAX_IDLE_SLEEP_MS
        return self.last_tx_ms + self.keepalive_s * 500 - now_ms

    def service(self):
        """Send PINGREQ at half the keepalive period and drain input."""
        now = self.clock.now_ms()
        if self.keepalive_s and self.ms_until_ping(now) <= 0:
            self.client.ping()
            self.last_tx_ms = now
        try:
            self.client.check_msg()
        finally:
            # check_msg() switches the socket to non-blocking/blocking mode;
            # restore the bounded timeout for later writes.
            if self.client is not None:
                self._apply_socket_timeout(self.client)

    @staticmethod
    def _close_client(client):
        sock = getattr(client, "sock", None)
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
            client.sock = None

    def close(self):
        if self.client is not None:
            self._close_client(self.client)
            self.client = None


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


class Sampler:
    """Takes ADC samples at SAMPLE_RATE_HZ, cooperatively from the main loop.

    Sampling is not timer-driven: while the loop is blocked (Wi-Fi or MQTT
    connection, socket timeout, retry delay) samples are not taken. Late
    sample slots are skipped, so sample_count in a reading can be lower than
    SAMPLE_RATE_HZ * PUBLISH_INTERVAL_S.
    """

    # Schedule kept in integer microseconds: MicroPython floats on rp2 are
    # single precision and cannot hold long uptimes with millisecond detail.

    def __init__(self, sensor, clock, rate_hz):
        self.sensor = sensor
        self.clock = clock
        self.period_us = max(1, int(1000000 / rate_hz + 0.5))
        self.aggregator = LevelAggregator()
        self.listeners = []  # objects with add(raw, voltage_v, level, in_range)
        self.next_us = clock.now_ms() * 1000

    def _sample(self):
        sample = self.sensor.read()
        self.aggregator.add(*sample)
        for listener in self.listeners:
            listener.add(*sample)

    def restart(self):
        """Discard collected samples and sample immediately."""
        self.aggregator.reset()
        self.next_us = self.clock.now_ms() * 1000

    def poll(self):
        """Take one sample if one is due."""
        now_us = self.clock.now_ms() * 1000
        if now_us < self.next_us:
            return
        self._sample()
        self.next_us += self.period_us
        if self.next_us <= now_us:
            # Late: skip missed slots instead of sampling in a burst.
            missed = (now_us - self.next_us) // self.period_us + 1
            self.next_us += missed * self.period_us

    def ms_until_next(self, now_ms):
        return -((now_ms * 1000 - self.next_us) // 1000)  # ceiling division

    def take_summary(self):
        """Summary of the samples since the previous call (at least one)."""
        if self.aggregator.count == 0:
            self._sample()
        summary = self.aggregator.summary()
        self.aggregator.reset()
        return summary


def build_payload(device_id, sequence, uptime_ms, summary):
    return json.dumps(
        {
            "device_id": device_id,
            "sequence": sequence,
            "uptime_ms": uptime_ms,
            "sample_count": summary["count"],
            "sensor_voltage_v": round(summary["voltage_mean_v"], 3),
            "sound_level_db_a_estimate": round(summary["level_db_a"], 1),
            "sound_level_db_a_min": round(summary["level_min_db_a"], 1),
            "sound_level_db_a_max": round(summary["level_max_db_a"], 1),
            "out_of_range_count": summary["out_of_range"],
            "in_range": summary["out_of_range"] == 0,
        }
    )


def format_reading(sequence, summary):
    text = "#%d n=%d raw=%d %.3f V %.1f dBA (estimate)" % (
        sequence,
        summary["count"],
        summary["last_raw"],
        summary["voltage_mean_v"],
        summary["level_db_a"],
    )
    if summary["count"] > 1:
        text += " min %.1f max %.1f" % (summary["level_min_db_a"], summary["level_max_db_a"])
    if summary["out_of_range"]:
        text += " OUT OF RANGE 0.6-2.6 V (%d/%d samples)" % (
            summary["out_of_range"],
            summary["count"],
        )
    return text


def service_lora(lora, logger):
    if lora is None:
        return
    try:
        lora.poll()
    except Exception as exc:
        logger("LoRa: error (%s)" % describe_error(exc))


def lora_wait(lora, now_ms):
    return MAX_IDLE_SLEEP_MS if lora is None else lora.ms_until_next(now_ms)


class SensorOnlyApp:
    """WIFI_SSID = None: sample the sensor and print readings; no network use."""

    def __init__(self, cfg, sensor, clock, logger=log, lora=None):
        self.clock = clock
        self.log = logger
        self.lora = lora
        self.sampler = Sampler(sensor, clock, cfg["SAMPLE_RATE_HZ"])
        if lora is not None:
            self.sampler.listeners.append(lora)
        self.interval_ms = int(cfg["PUBLISH_INTERVAL_S"] * 1000)
        self.sequence = 0
        self.next_read_ms = clock.now_ms() + self.interval_ms

    def step(self):
        # Close the interval before sampling, so a sample due at the interval
        # boundary belongs to the next interval.
        now = self.clock.now_ms()
        if now >= self.next_read_ms:
            self.sequence += 1
            self.log(format_reading(self.sequence, self.sampler.take_summary()))
            self.next_read_ms += self.interval_ms
            if self.next_read_ms <= now:
                self.next_read_ms = now + self.interval_ms
        self.sampler.poll()
        service_lora(self.lora, self.log)
        now = self.clock.now_ms()
        wait = min(
            self.next_read_ms - now,
            self.sampler.ms_until_next(now),
            lora_wait(self.lora, now),
            MAX_IDLE_SLEEP_MS,
        )
        return max(wait, MIN_IDLE_SLEEP_MS)

    def run(self, max_steps=None):
        steps = 0
        while max_steps is None or steps < max_steps:
            steps += 1
            try:
                wait = self.step()
            except Exception as exc:
                self.log("Sensor error (%s)" % describe_error(exc))
                wait = self.interval_ms
            self.clock.sleep_ms(wait)


class App:
    def __init__(self, cfg, device_id, topic, sensor, wifi, mqtt, clock, logger=log,
                 lora=None):
        self.device_id = device_id
        self.topic = topic
        self.topic_bytes = topic.encode()
        self.sensor = sensor
        self.wifi = wifi
        self.mqtt = mqtt
        self.clock = clock
        self.log = logger
        self.interval_ms = int(cfg["PUBLISH_INTERVAL_S"] * 1000)
        self.retry_ms = int(cfg["RETRY_DELAY_S"] * 1000)
        self.start_ms = clock.now_ms()
        self.sequence = 0
        self.sampler = Sampler(sensor, clock, cfg["SAMPLE_RATE_HZ"])
        self.lora = lora
        if lora is not None:
            self.sampler.listeners.append(lora)
        self.next_publish_ms = self.start_ms

    def publish_reading(self):
        summary = self.sampler.take_summary()
        self.sequence += 1
        uptime_ms = self.clock.now_ms() - self.start_ms
        payload = build_payload(self.device_id, self.sequence, uptime_ms, summary)
        self.log(format_reading(self.sequence, summary))
        self.mqtt.publish(self.topic_bytes, payload.encode())

    def step(self):
        """Run one loop iteration; return the number of ms to sleep."""
        service_lora(self.lora, self.log)
        if not self.wifi.is_connected():
            if self.mqtt.connected:
                self.log("Wi-Fi: link lost; closing MQTT connection")
                self.mqtt.close()
            if not self.wifi.connect():
                self.log("Retrying in %d s" % (self.retry_ms // 1000))
                return self.retry_ms

        if not self.mqtt.connected:
            self.log("MQTT: connecting to %s:%s" % (self.mqtt.server, self.mqtt.port))
            try:
                self.mqtt.connect()
            except Exception as exc:
                self.mqtt.close()
                self.log("MQTT: connection failed (%s); retrying in %d s"
                         % (describe_error(exc), self.retry_ms // 1000))
                return self.retry_ms
            self.log("MQTT: connected")
            # First reading one interval after connection, from fresh samples.
            self.sampler.restart()
            self.next_publish_ms = self.clock.now_ms() + self.interval_ms

        try:
            now = self.clock.now_ms()
            if now >= self.next_publish_ms:
                self.publish_reading()
                self.next_publish_ms += self.interval_ms
                if self.next_publish_ms <= now:
                    # Fell behind (slow network): skip missed slots, no burst.
                    self.next_publish_ms = now + self.interval_ms
            self.sampler.poll()
            self.mqtt.service()
        except Exception as exc:
            self.mqtt.close()
            self.log("MQTT: link error (%s); reconnecting in %d s"
                     % (describe_error(exc), self.retry_ms // 1000))
            return self.retry_ms

        now = self.clock.now_ms()
        wait = min(
            self.next_publish_ms - now,
            self.sampler.ms_until_next(now),
            lora_wait(self.lora, now),
            self.mqtt.ms_until_ping(now),
            MAX_IDLE_SLEEP_MS,
        )
        return max(wait, MIN_IDLE_SLEEP_MS)

    def run(self, max_steps=None):
        steps = 0
        try:
            while max_steps is None or steps < max_steps:
                steps += 1
                try:
                    wait = self.step()
                except Exception as exc:
                    self.log("Unexpected error (%s); retrying in %d s"
                             % (describe_error(exc), self.retry_ms // 1000))
                    wait = self.retry_ms
                self.clock.sleep_ms(wait)
        finally:
            self.mqtt.close()


def boot_payload(cfg, reset_cause):
    flags = 0
    if wifi_enabled(cfg):
        flags |= wioe5.BOOT_FLAG_WIFI
    if cfg["CAL_A"] != 1.0 or cfg["CAL_B"] != 0.0:
        flags |= wioe5.BOOT_FLAG_CALIBRATED
    if cfg["LORA_ADR"]:
        flags |= wioe5.BOOT_FLAG_ADR
    return wioe5.encode_boot(APP_VERSION, reset_cause, cfg["LORA_SLOT_S"],
                             cfg["LORA_SLOTS_PER_UPLINK"], cfg["SAMPLE_RATE_HZ"],
                             flags, cfg["LORA_DATA_RATE"])


def build_lora(cfg, clock, uart=None, logger=log, reset_cause=None):
    """Return a LoraUplink, or None when LoRa is disabled in the configuration.

    If the Wio-E5 does not answer at start-up, the uplink stays in the
    "unconfigured" state and retries the configuration periodically.
    """
    if not cfg["LORA_ENABLED"]:
        return None
    if uart is None:
        uart = wioe5.make_uart(cfg)
    boot = None
    if cfg["LORA_BOOT_MESSAGE"]:
        if reset_cause is None:
            reset_cause = wioe5.reset_cause_code()
        boot = boot_payload(cfg, reset_cause)
    lora = wioe5.LoraUplink(cfg, wioe5.AtModem(uart, clock, logger), clock, logger,
                            boot_payload=boot)
    lora.start()
    return lora


def main():
    log("%s %d.%d starting" % ((APP_NAME,) + APP_VERSION))
    try:
        import secrets as user_config
    except ImportError:
        user_config = None
    if user_config is None or not hasattr(user_config, "WIFI_SSID"):
        log("No secrets.py with WIFI_SSID found: using defaults (sensor-only mode).")
        log("For Wi-Fi/MQTT, copy config.example.py to secrets.py and set WIFI_SSID.")
        user_config = None

    cfg = load_config(user_config) if user_config is not None else dict(DEFAULTS)
    errors = validate_config(cfg)
    if errors:
        log("ERROR: invalid configuration in secrets.py:")
        for error in errors:
            log("  - " + error)
        return

    if not wifi_enabled(cfg):
        for line in describe_config(cfg, None, None):
            log(line)
        clock = Clock()
        sensor = SEN0232(cfg["ADC_GPIO"], cfg["ADC_REFERENCE_V"], cfg["CAL_A"], cfg["CAL_B"])
        lora = build_lora(cfg, clock)
        try:
            SensorOnlyApp(cfg, sensor, clock, lora=lora).run()
        except KeyboardInterrupt:
            log("Stopped by user (Ctrl-C).")
        return

    client_class = load_mqtt_client_class()
    if client_class is None:
        log("ERROR: umqtt.simple is not installed on the Pico.")
        log("Install it with mip (see README.md) or copy simple.py to /lib/umqtt/.")
        return

    device_id = cfg["DEVICE_ID"] or default_device_id()
    topic = cfg["MQTT_TOPIC"] or default_topic(device_id)
    for line in describe_config(cfg, device_id, topic):
        log(line)

    clock = Clock()
    sensor = SEN0232(cfg["ADC_GPIO"], cfg["ADC_REFERENCE_V"], cfg["CAL_A"], cfg["CAL_B"])
    lora = build_lora(cfg, clock)
    wifi = WiFi(cfg, clock)
    mqtt = MqttLink(cfg, client_class, device_id, clock)
    app = App(cfg, device_id, topic, sensor, wifi, mqtt, clock, lora=lora)
    try:
        app.run()
    except KeyboardInterrupt:
        log("Stopped by user (Ctrl-C).")


if __name__ == "__main__":
    main()
