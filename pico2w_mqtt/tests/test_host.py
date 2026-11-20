"""Host-side (CPython) checks with fakes; no Pico, Wi-Fi or broker required.

Run from code/pico2w_mqtt:  python3 -m unittest discover -s tests -v

These tests exercise logic only. They do not prove MicroPython runtime
compatibility or hardware behaviour.
"""

import io
import json
import math
import shutil
import subprocess
import os
import sys
import types
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import main  # noqa: E402
import sen0232  # noqa: E402
import wioe5  # noqa: E402

WIFI_PW = "wifi-secret-123"
MQTT_PW = "mqtt-secret-456"

TICKS_PERIOD = 1 << 30


class FakeTime:
    """ticks_ms() that wraps like MicroPython; sleep advances virtual time."""

    def __init__(self, start=0):
        self.t = start

    def ticks_ms(self):
        return self.t % TICKS_PERIOD

    @staticmethod
    def ticks_diff(a, b):
        half = TICKS_PERIOD // 2
        return ((a - b + half) % TICKS_PERIOD) - half

    def sleep_ms(self, ms):
        self.t += ms


def make_clock(start=0):
    ft = FakeTime(start)
    return ft, main.Clock(ft.ticks_ms, ft.ticks_diff, ft.sleep_ms)


class FakeWlan:
    def __init__(self, outcomes):
        # outcomes: list of "ok" / "fail" / "badpw" consumed per connect()
        self.outcomes = list(outcomes)
        self.connected = False
        self._status = 0
        self.connect_calls = 0
        self.passwords_seen = []

    def active(self, flag=None):
        return True

    def isconnected(self):
        return self.connected

    def status(self):
        return self._status

    def connect(self, ssid, password=None):
        self.connect_calls += 1
        self.passwords_seen.append(password)
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if outcome == "ok":
            self.connected = True
            self._status = 3
        elif outcome == "badpw":
            self._status = -3
        else:
            self._status = 1  # stays "connecting" until timeout

    def disconnect(self):
        self.connected = False
        self._status = 0

    def ifconfig(self):
        return ("192.0.2.10", "255.255.255.0", "192.0.2.1", "192.0.2.1")


class FakeSock:
    def __init__(self):
        self.closed = False
        self.timeouts = []

    def settimeout(self, t):
        self.timeouts.append(t)

    def close(self):
        self.closed = True


class Broker:
    """Shared script/record for FakeClient instances."""

    def __init__(self, connect_fail=0, publish_fail_at=(), legacy=False):
        self.connect_fail = connect_fail
        self.publish_fail_at = set(publish_fail_at)
        self.legacy = legacy
        self.messages = []
        self.publish_calls = 0
        self.pings = 0
        self.clients = []
        self.sockets = []
        self.init_args = []

    def client_class(self):
        broker = self

        class FakeClient:
            def __init__(self, client_id, server, port=0, user=None, password=None,
                         keepalive=0, ssl=None, ssl_params=None):
                broker.init_args.append((client_id, server, port, user, password, keepalive))
                self.sock = None
                broker.clients.append(self)

            def connect(self, clean_session=True, **kw):
                if broker.legacy and "timeout" in kw:
                    raise TypeError("unexpected keyword argument 'timeout'")
                self.sock = FakeSock()
                broker.sockets.append(self.sock)
                if broker.connect_fail > 0:
                    broker.connect_fail -= 1
                    raise OSError(113, "EHOSTUNREACH")
                return False

            def set_callback(self, cb):
                self.cb = cb

            def publish(self, topic, msg, retain=False, qos=0):
                broker.publish_calls += 1
                if broker.publish_calls in broker.publish_fail_at:
                    raise OSError(104, "ECONNRESET")
                broker.messages.append((topic, msg, retain, qos))

            def ping(self):
                broker.pings += 1

            def check_msg(self):
                return None

        return FakeClient


class FakeSensor:
    def __init__(self, voltage=1.2):
        self.voltage = voltage

    def read(self):
        raw = int(round(self.voltage * 65535 / 3.3))
        v, level, ok = sen0232.convert(raw, 3.3)
        return raw, v, level, ok


def base_cfg(**over):
    cfg = dict(main.DEFAULTS)
    cfg.update(
        WIFI_SSID="bench-net",
        WIFI_PASSWORD=WIFI_PW,
        MQTT_BROKER="192.0.2.50",
        MQTT_USER="noisenet",
        MQTT_PASSWORD=MQTT_PW,
        CONNECT_TIMEOUT_S=2,
        RETRY_DELAY_S=3,
    )
    cfg.update(over)
    return cfg


def build_app(cfg, wlan, broker, start=0):
    logs = []
    ft, clock = make_clock(start)
    wifi = main.WiFi(cfg, clock, wlan=wlan, logger=logs.append)
    mqtt = main.MqttLink(cfg, broker.client_class(), "pico2w-test", clock, logger=logs.append)
    app = main.App(cfg, "pico2w-test", "noisenet/pico2w-test/sound",
                   FakeSensor(), wifi, mqtt, clock, logger=logs.append)
    return app, ft, logs


class ConversionTests(unittest.TestCase):
    def test_reference_points(self):
        for v, db in ((0.6, 30.0), (1.2, 60.0), (2.6, 130.0)):
            self.assertAlmostEqual(sen0232.voltage_to_db_a(v), db, places=9)

    def test_reference_scaling(self):
        self.assertAlmostEqual(sen0232.raw_to_voltage(65535, 3.3), 3.3)
        self.assertAlmostEqual(sen0232.raw_to_voltage(0, 3.3), 0.0)
        self.assertAlmostEqual(sen0232.raw_to_voltage(32768, 3.0), 32768 * 3.0 / 65535)
        self.assertAlmostEqual(sen0232.raw_to_voltage(65535, 3.25), 3.25)
        with self.assertRaises(ValueError):
            sen0232.raw_to_voltage(65536, 3.3)

    def test_calibration(self):
        self.assertEqual(sen0232.apply_calibration(60.0), 60.0)
        self.assertAlmostEqual(sen0232.apply_calibration(60.0, 1.1, -2.0), 64.0)
        raw = int(round(1.2 * 65535 / 3.3))
        v, level, ok = sen0232.convert(raw, 3.3, 1.1, -2.0)
        self.assertAlmostEqual(level, 1.1 * v * 50 - 2.0)
        self.assertTrue(ok)

    def test_out_of_range_not_clamped(self):
        self.assertTrue(sen0232.in_measurement_range(0.6))
        self.assertTrue(sen0232.in_measurement_range(2.6))
        self.assertFalse(sen0232.in_measurement_range(0.59))
        self.assertFalse(sen0232.in_measurement_range(2.61))
        raw = int(round(2.9 * 65535 / 3.3))
        v, level, ok = sen0232.convert(raw, 3.3)
        self.assertFalse(ok)
        self.assertGreater(level, 140.0)

    def test_sensor_class_with_fake_adc(self):
        adc = types.SimpleNamespace(read_u16=lambda: 23831)  # ~1.2 V at 3.3 V ref
        raw, v, level, ok = sen0232.SEN0232(26, 3.3, adc=adc).read()
        self.assertEqual(raw, 23831)
        self.assertAlmostEqual(level, 60.0, places=1)
        self.assertTrue(ok)


class ConfigTests(unittest.TestCase):
    def test_valid_config(self):
        self.assertEqual(main.validate_config(base_cfg()), [])

    def test_missing_and_placeholder(self):
        errors = main.validate_config(dict(main.DEFAULTS, WIFI_SSID=""))
        joined = " ".join(errors)
        self.assertIn("WIFI_SSID must not be empty", joined)
        self.assertIn("MQTT_BROKER is required", joined)
        errors = main.validate_config(base_cfg(WIFI_SSID="<your-ssid>"))
        self.assertTrue(any("placeholder" in e for e in errors))

    def test_rejections(self):
        cases = [
            dict(MQTT_BROKER="localhost"),
            dict(MQTT_PORT=0),
            dict(MQTT_PORT="1883"),
            dict(ADC_GPIO=29),
            dict(PUBLISH_INTERVAL_S=0),
            dict(MQTT_USER=None),  # password without user
            dict(MQTT_TOPIC="noisenet/#"),
            dict(DEVICE_ID="a/b"),
            dict(MQTT_KEEPALIVE_S=8, SOCKET_TIMEOUT_S=5),
            dict(WIFI_COUNTRY="FRA"),
        ]
        for over in cases:
            self.assertNotEqual(main.validate_config(base_cfg(**over)), [], over)

    def test_errors_and_description_never_echo_secrets(self):
        cfg = base_cfg(MQTT_USER=None)
        text = "\n".join(main.validate_config(cfg))
        text += "\n".join(main.describe_config(base_cfg(), "id", "t"))
        self.assertNotIn(WIFI_PW, text)
        self.assertNotIn(MQTT_PW, text)
        self.assertIn("<set>", text)

    def test_default_topic(self):
        self.assertEqual(main.default_topic("pico2w-ab12"), "noisenet/pico2w-ab12/sound")


class ScriptedSensor:
    """Alternates 0.62 V and 2.58 V (31 / 129 dBA)."""

    def __init__(self):
        self.reads = 0

    def read(self):
        self.reads += 1
        v = 0.62 if self.reads % 2 else 2.58
        raw = int(round(v * 65535 / 3.3))
        return (raw,) + sen0232.convert(raw, 3.3)


class AggregationTests(unittest.TestCase):
    def test_energy_average_min_max(self):
        agg = sen0232.LevelAggregator()
        agg.add(100, 1.0, 60.0, True)
        agg.add(200, 1.4, 70.0, True)
        agg.add(300, 2.9, 145.0, False)
        s = agg.summary()
        self.assertEqual(s["count"], 3)
        self.assertEqual(s["last_raw"], 300)
        self.assertAlmostEqual(s["voltage_mean_v"], 1.7666666, places=5)
        expected = 10 * math.log10((10 ** 6 + 10 ** 7 + 10 ** 14.5) / 3)
        self.assertAlmostEqual(s["level_db_a"], expected, places=6)
        self.assertEqual((s["level_min_db_a"], s["level_max_db_a"]), (60.0, 145.0))
        self.assertEqual(s["out_of_range"], 1)

    def test_equal_levels_average_to_same_level(self):
        agg = sen0232.LevelAggregator()
        for _ in range(10):
            agg.add(0, 1.2, 60.0, True)
        self.assertAlmostEqual(agg.summary()["level_db_a"], 60.0, places=9)

    def test_empty_summary_rejected(self):
        with self.assertRaises(ValueError):
            sen0232.LevelAggregator().summary()

    def test_sample_rate_validation(self):
        bad = [dict(SAMPLE_RATE_HZ=0), dict(SAMPLE_RATE_HZ=500),
               dict(SAMPLE_RATE_HZ=0.5, PUBLISH_INTERVAL_S=1)]
        for over in bad:
            self.assertNotEqual(main.validate_config(dict(main.DEFAULTS, **over)), [], over)
        ok = dict(main.DEFAULTS, SAMPLE_RATE_HZ=0.1, PUBLISH_INTERVAL_S=10)
        self.assertEqual(main.validate_config(ok), [])

    def test_mqtt_payload_with_sampling(self):
        broker = Broker()
        cfg = base_cfg(SAMPLE_RATE_HZ=20)
        app, ft, logs = build_app(cfg, FakeWlan(["ok"]), broker)
        app.sampler.sensor = ScriptedSensor()
        AppTests.run_app(self, app, ft, 3050)
        p = json.loads(broker.messages[1][1].decode())
        self.assertEqual(p["sample_count"], 20)
        self.assertEqual(p["out_of_range_count"], 0)
        self.assertEqual((p["sound_level_db_a_min"], p["sound_level_db_a_max"]), (31.0, 129.0))
        expected = 10 * math.log10((10 ** 3.1 + 10 ** 12.9) / 2)  # about 126.0
        self.assertAlmostEqual(p["sound_level_db_a_estimate"], expected, delta=0.1)
        self.assertAlmostEqual(p["sensor_voltage_v"], 1.6, places=3)

    def test_late_samples_skipped_not_bursted(self):
        ft, clock = make_clock()
        sensor = ScriptedSensor()
        sampler = main.Sampler(sensor, clock, 10)
        sampler.poll()
        ft.t += 1000  # loop blocked for 1 s
        sampler.poll()
        sampler.poll()
        self.assertEqual(sensor.reads, 2)
        self.assertGreater(sampler.ms_until_next(clock.now_ms()), 0)


APP_KEY = "00112233445566778899AABBCCDDEEFF"


class FakeWioUart:
    """Simulates the Wio-E5 AT firmware replies (immediate responses)."""

    def __init__(self, joins=("ok",), sends=None, silent=False, boot_writes=0):
        self.joins = list(joins)
        self.sends = list(sends or [])
        self.silent = silent
        self.boot_writes = boot_writes  # commands ignored while the module boots
        self.rx = b""
        self.commands = []
        self.uplinks = []  # measurement uplinks (port 8)
        self.sent = []  # (port, payload) of every uplink
        self.port = 8

    def any(self):
        return len(self.rx)

    def read(self, n=None):
        data, self.rx = self.rx[:n], self.rx[n:]
        return data

    def _reply(self, *lines):
        self.rx += b"".join(l.encode() + b"\r\n" for l in lines)

    def write(self, data):
        cmd = data.decode().strip()
        self.commands.append(cmd)
        if self.silent:
            return
        if self.boot_writes > 0:
            self.boot_writes -= 1
            return
        if cmd == "AT":
            self._reply("+AT: OK")
        elif cmd.startswith("AT+KEY=APPKEY"):
            self._reply("+KEY: APPKEY " + cmd.split('"')[1])
        elif cmd.startswith("AT+JOIN"):
            outcome = self.joins.pop(0) if self.joins else "ok"
            if outcome == "ok":
                self._reply("+JOIN: Start", "+JOIN: NORMAL", "+JOIN: Network joined",
                            "+JOIN: NetID 000013 DevAddr 26:01:5F:66", "+JOIN: Done")
            else:
                self._reply("+JOIN: Start", "+JOIN: NORMAL", "+JOIN: Join failed",
                            "+JOIN: Done")
        elif cmd.startswith("AT+MSGHEX="):
            outcome = self.sends.pop(0) if self.sends else "ok"
            if outcome == "ok":
                payload = bytes.fromhex(cmd.split('"')[1])
                self.sent.append((self.port, payload))
                if self.port == 8:
                    self.uplinks.append(payload)
                self._reply("+MSGHEX: Start", "+MSGHEX: Done")
            elif outcome == "nojoin":
                self._reply("+MSGHEX: Please join network first")
        else:
            if cmd.startswith("AT+PORT="):
                self.port = int(cmd.split("=")[1])
            key = cmd[3:].split("=")[0]
            self._reply("+%s: %s" % (key, cmd.split("=", 1)[1]))


def lora_cfg(**over):
    cfg = dict(main.DEFAULTS, LORA_ENABLED=True, LORA_APP_KEY=APP_KEY,
               SAMPLE_RATE_HZ=10, LORA_JOIN_RETRY_S=30, LORA_SLOTS_PER_UPLINK=2)
    cfg.update(over)
    return cfg


class LoraPayloadTests(unittest.TestCase):
    def summary(self, **over):
        s = {"count": 600, "level_max_db_a": 72.34, "out_of_range": 0}
        s.update(over)
        return s

    def test_batch_round_trip(self):
        slots = [self.summary(level_max_db_a=v) for v in (41.04, 55.5, 72.34, 90.0, 38.2)]
        data = wioe5.encode_batch(slots, 300, 60, 600)
        self.assertEqual(len(data), 15)
        self.assertEqual(data.hex(), "15062c019a022b02d30384017e0000")
        d = wioe5.decode_batch(data)
        self.assertEqual((d["sequence"], d["slot_s"]), (44, 60))
        self.assertEqual([x["max_db_a_estimate"] for x in d["slots"]],
                         [41.0, 55.5, 72.3, 90.0, 38.2])
        self.assertEqual([x["end_offset_s"] for x in d["slots"]], [-240, -180, -120, -60, 0])

    def test_flags_and_limits(self):
        slots = [None, self.summary(out_of_range=2), self.summary(count=100),
                 self.summary(level_max_db_a=-5.0)]
        d = wioe5.decode_batch(wioe5.encode_batch(slots, 1, 60, 600))
        self.assertIsNone(d["slots"][0]["max_db_a_estimate"])
        self.assertEqual([x["out_of_range"] for x in d["slots"]], [False, True, False, False])
        self.assertEqual([x["sample_gaps"] for x in d["slots"]], [True, False, True, False])
        self.assertEqual(d["slots"][3]["max_db_a_estimate"], 0.0)
        with self.assertRaises(ValueError):
            wioe5.encode_batch([None] * 9, 1, 60, 600)

    def run_formatter(self, data, port):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not installed")
        script = ("const f=require(%r);"
                  "console.log(JSON.stringify(f.decodeUplink({bytes:%s,fPort:%d})))"
                  % (os.path.join(os.path.dirname(HERE), "ttn_formatter.js"),
                     list(data), port))
        out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_ttn_formatter_matches_python_decoder(self):
        slots = [None, self.summary(out_of_range=1), self.summary(count=10),
                 self.summary(level_max_db_a=129.04), self.summary(level_max_db_a=30.0)]
        data = wioe5.encode_batch(slots, 7, 60, 600)
        self.assertEqual(self.run_formatter(data, 8)["data"], wioe5.decode_batch(data))
        boot = wioe5.encode_boot((1, 0), wioe5.RESET_WATCHDOG, 60, 5, 20, 0x06, None)
        self.assertEqual(self.run_formatter(boot, 9)["data"], wioe5.decode_boot(boot))
        self.assertIn("errors", self.run_formatter(b"\x01\x02\x03", 8))
        self.assertIn("errors", self.run_formatter(data, 9))
        self.assertIn("errors", self.run_formatter(data, 10))

    def test_boot_round_trip(self):
        data = wioe5.encode_boot((1, 2), wioe5.RESET_POWER_ON, 60, 5, 20, 0x01, 5)
        self.assertEqual(data.hex(), "01010201060507d00105")
        self.assertEqual(wioe5.decode_boot(data), {
            "message": "boot", "firmware_version": "1.2", "reset_cause": "power-on",
            "slot_s": 60, "slots_per_uplink": 5, "sample_rate_hz": 20.0,
            "wifi_enabled": True, "calibration_applied": False, "adr": False,
            "data_rate": 5})
        d = wioe5.decode_boot(wioe5.encode_boot((1, 0), 0, 600, 8, 0.01, 0, None))
        self.assertEqual((d["sample_rate_hz"], d["data_rate"], d["reset_cause"]),
                         (0.01, None, "unknown"))
        with self.assertRaises(ValueError):
            wioe5.decode_boot(data[:9])

    def test_boot_payload_flags_from_config(self):
        cfg = dict(main.DEFAULTS, CAL_A=1.05, LORA_ADR=True, LORA_DATA_RATE=None)
        d = wioe5.decode_boot(main.boot_payload(cfg, wioe5.RESET_WATCHDOG))
        self.assertEqual(d["firmware_version"], "%d.%d" % main.APP_VERSION)
        self.assertTrue(d["calibration_applied"] and d["adr"])
        self.assertFalse(d["wifi_enabled"])
        self.assertEqual((d["reset_cause"], d["data_rate"]),
                         ("watchdog or machine.reset()", None))

    def test_airtime(self):
        self.assertAlmostEqual(wioe5.airtime_ms(7, 17), 51.5, delta=0.5)
        self.assertAlmostEqual(wioe5.airtime_ms(12, 17), 1318.9, delta=1.0)
        self.assertAlmostEqual(wioe5.daily_airtime_s(5, 60, 4), 74.2, delta=1.0)
        self.assertLess(wioe5.daily_airtime_s(5, 300, wioe5.payload_length(5)), 30)


class LoraUplinkTests(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.ft, self.clock = make_clock()

    def build(self, uart, **over):
        cfg = lora_cfg(**over)
        self.assertEqual(main.validate_config(cfg), [])
        return main.build_lora(cfg, self.clock, uart=uart, logger=self.logs.append,
                               reset_cause=wioe5.RESET_POWER_ON)

    def run_for(self, app, ms):
        end = self.ft.t + ms
        while self.ft.t < end:
            self.clock.sleep_ms(app.step())

    def test_configure_commands_and_secret_not_logged(self):
        uart = FakeWioUart()
        lora = self.build(uart)
        self.assertIsNotNone(lora)
        self.assertEqual(uart.commands[:7], [
            "AT", "AT+MODE=LWOTAA", "AT+DR=EU868", "AT+CH=NUM,0-2", "AT+PORT=8",
            "AT+ADR=OFF", "AT+DR=5"])
        self.assertIn('AT+KEY=APPKEY,"%s"' % APP_KEY, uart.commands)
        self.assertFalse(any(APP_KEY in l for l in self.logs))
        text = "\n".join(main.describe_config(lora_cfg(), None, None))
        self.assertNotIn(APP_KEY, text)
        self.assertIn("exceeds the TTN Fair Use Policy", text)  # 9 bytes every 120 s
        five = "\n".join(main.describe_config(lora_cfg(LORA_SLOTS_PER_UPLINK=5), None, None))
        self.assertIn("about 19 s/day at DR5", five)  # 15 bytes every 300 s
        self.assertNotIn("exceeds", five)

    def test_module_still_booting_at_startup(self):
        uart = FakeWioUart(boot_writes=5)  # first 5 "AT" probes unanswered
        lora = self.build(uart)
        self.assertEqual(lora.state, lora.UNJOINED)
        self.assertEqual(uart.commands[:6], ["AT"] * 6)
        self.assertTrue(any("answered after 6 attempts" in l for l in self.logs))
        self.assertLess(self.ft.t, 5000)  # 6 probes of 500 ms

    def test_silent_modem_retried_until_it_answers(self):
        uart = FakeWioUart(silent=True)
        lora = self.build(uart)
        self.assertEqual(lora.state, lora.UNCONFIGURED)
        self.assertGreaterEqual(self.ft.t, 10000)  # full start-up probe
        self.assertTrue(any("no reply from Wio-E5" in l for l in self.logs))
        self.assertTrue(any("retrying the Wio-E5 configuration" in l for l in self.logs))
        app = main.SensorOnlyApp(lora_cfg(), FakeSensor(), self.clock,
                                 logger=self.logs.append, lora=lora)
        self.run_for(app, 30000)
        self.assertEqual(lora.state, lora.UNCONFIGURED)  # no retry before 60 s
        uart.silent = False  # module now answers (e.g. reconnected)
        self.run_for(app, 45000)
        self.assertIn(lora.state, (lora.JOINED, lora.SENDING))
        self.assertIn("AT+JOIN", uart.commands)

    def test_join_retry_then_batched_minute_maxima(self):
        uart = FakeWioUart(joins=["fail", "ok"])
        lora = self.build(uart)
        sensor = ScriptedSensor()  # alternates 31 / 129 dBA
        app = main.SensorOnlyApp(lora_cfg(), sensor, self.clock,
                                 logger=self.logs.append, lora=lora)
        self.run_for(app, 365000)
        joins = [c for c in uart.commands if c == "AT+JOIN"]
        self.assertEqual(len(joins), 2)
        self.assertTrue(any("join failed" in l for l in self.logs))
        self.assertEqual(len(uart.uplinks), 3)  # batches ending at 120, 240, 360 s
        decoded = [wioe5.decode_batch(u) for u in uart.uplinks]
        self.assertEqual([d["sequence"] for d in decoded], [1, 2, 3])
        for d in decoded:
            self.assertEqual(d["slot_s"], 60)
            self.assertEqual(len(d["slots"]), 2)
            for slot in d["slots"]:
                self.assertEqual(slot["max_db_a_estimate"], 129.0)
                self.assertFalse(slot["out_of_range"] or slot["sample_gaps"])

    def test_window_closed_while_unjoined_is_not_sent(self):
        uart = FakeWioUart(joins=["fail", "fail", "fail"])
        lora = self.build(uart, LORA_JOIN_RETRY_S=100)
        app = main.SensorOnlyApp(lora_cfg(), FakeSensor(), self.clock,
                                 logger=self.logs.append, lora=lora)
        self.run_for(app, 121000)
        self.assertEqual(uart.uplinks, [])
        self.assertTrue(any("batch of 2 slots not sent (state: unjoined)" in l
                            for l in self.logs))

    def test_please_join_first_triggers_rejoin(self):
        uart = FakeWioUart(joins=["ok", "ok"], sends=["nojoin", "ok"])
        lora = self.build(uart, LORA_BOOT_MESSAGE=False)
        app = main.SensorOnlyApp(lora_cfg(), FakeSensor(), self.clock,
                                 logger=self.logs.append, lora=lora)
        self.run_for(app, 245000)
        self.assertEqual(len([c for c in uart.commands if c == "AT+JOIN"]), 2)
        self.assertEqual(len(uart.uplinks), 1)
        self.assertEqual(wioe5.decode_batch(uart.uplinks[0])["sequence"], 2)

    def test_boot_message_once_after_join_then_port_restored(self):
        uart = FakeWioUart(joins=["fail", "ok"])
        lora = self.build(uart)
        app = main.SensorOnlyApp(lora_cfg(), ScriptedSensor(), self.clock,
                                 logger=self.logs.append, lora=lora)
        self.run_for(app, 250000)
        ports = [p for p, _ in uart.sent]
        self.assertEqual(ports, [9, 8, 8])  # boot after the join, then two batches
        boot = wioe5.decode_boot(uart.sent[0][1])
        self.assertEqual((boot["reset_cause"], boot["slot_s"], boot["slots_per_uplink"]),
                         ("power-on", 60, 2))
        self.assertEqual(boot["sample_rate_hz"], 10.0)
        self.assertTrue(any("boot message sent" in l for l in self.logs))
        # Port 9 selected for the boot message only, port 8 restored before the batch.
        port_cmds = [c for c in uart.commands if c.startswith("AT+PORT=")]
        self.assertEqual(port_cmds, ["AT+PORT=8", "AT+PORT=9", "AT+PORT=8"])
        self.assertEqual(wioe5.decode_batch(uart.uplinks[0])["sequence"], 1)

    def test_boot_message_not_retried(self):
        uart = FakeWioUart(sends=["nojoin", "ok", "ok"])
        lora = self.build(uart)
        app = main.SensorOnlyApp(lora_cfg(), FakeSensor(), self.clock,
                                 logger=self.logs.append, lora=lora)
        self.run_for(app, 130000)
        self.assertEqual([p for p, _ in uart.sent], [8])
        self.assertTrue(any("boot message not sent" in l for l in self.logs))

    def test_boot_message_disabled(self):
        uart = FakeWioUart()
        lora = self.build(uart, LORA_BOOT_MESSAGE=False)
        app = main.SensorOnlyApp(lora_cfg(), FakeSensor(), self.clock,
                                 logger=self.logs.append, lora=lora)
        self.run_for(app, 130000)
        self.assertEqual([p for p, _ in uart.sent], [8])
        self.assertNotIn("AT+PORT=9", uart.commands)

    def test_lora_validation(self):
        bad = [dict(LORA_TX_GPIO=2), dict(LORA_UART_ID=2), dict(LORA_APP_KEY="12"),
               dict(LORA_JOIN_EUI="52:69"), dict(LORA_DATA_RATE=6), dict(LORA_PORT=0),
               dict(LORA_INTERVAL_S=60), dict(LORA_SLOT_S=65), dict(LORA_SLOT_S=5),
               dict(LORA_SLOTS_PER_UPLINK=9), dict(LORA_SLOTS_PER_UPLINK=0),
               dict(LORA_ENABLED="yes"), dict(LORA_BOOT_PORT=8), dict(LORA_BOOT_PORT=0),
               dict(LORA_BOOT_MESSAGE="on"),
               dict(ADC_GPIO=28, LORA_TX_GPIO=28, LORA_RX_GPIO=29)]
        for over in bad:
            self.assertNotEqual(main.validate_config(lora_cfg(**over)), [], over)
        ok = lora_cfg(LORA_UART_ID=1, LORA_TX_GPIO=20, LORA_RX_GPIO=21,
                      LORA_DATA_RATE=None, LORA_ADR=True, LORA_JOIN_EUI="526973696E674846")
        self.assertEqual(main.validate_config(ok), [])


class SensorOnlyTests(unittest.TestCase):
    def test_prints_readings_at_interval_without_network(self):
        logs = []
        ft, clock = make_clock()
        cfg = dict(main.DEFAULTS)
        self.assertEqual(main.validate_config(cfg), [])
        app = main.SensorOnlyApp(cfg, FakeSensor(2.9), clock, logger=logs.append)
        end = ft.t + 3050
        while ft.t < end:
            clock.sleep_ms(app.step())
        self.assertEqual(len(logs), 3)  # t = 1, 2, 3 s
        self.assertTrue(logs[0].startswith("#1 n=1 raw="))
        self.assertIn("OUT OF RANGE", logs[0])

    def test_sample_rate_aggregates_samples_per_reading(self):
        logs = []
        ft, clock = make_clock()
        cfg = dict(main.DEFAULTS, SAMPLE_RATE_HZ=50, PUBLISH_INTERVAL_S=2)
        self.assertEqual(main.validate_config(cfg), [])
        sensor = ScriptedSensor()
        app = main.SensorOnlyApp(cfg, sensor, clock, logger=logs.append)
        end = ft.t + 4050
        while ft.t < end:
            clock.sleep_ms(app.step())
        self.assertEqual(len(logs), 2)
        self.assertTrue(logs[0].startswith("#1 n=100 "), logs[0])
        self.assertIn("min 31.0 max 129.0", logs[0])
        self.assertNotIn("OUT OF RANGE", logs[0])
        self.assertTrue(logs[1].startswith("#2 n=100 "), logs[1])
        self.assertEqual(sensor.reads - app.sampler.aggregator.count, 200)

    def test_sensor_settings_still_validated(self):
        errors = main.validate_config(dict(main.DEFAULTS, ADC_GPIO=29))
        self.assertTrue(any("ADC_GPIO" in e for e in errors))


class ClockTests(unittest.TestCase):
    def test_monotonic_across_wrap(self):
        ft, clock = make_clock(start=TICKS_PERIOD - 1500)
        values = []
        for _ in range(10):
            clock.sleep_ms(500)
            values.append(clock.now_ms())
        self.assertEqual(values, [500 * (i + 1) for i in range(10)])


class AppTests(unittest.TestCase):
    def run_app(self, app, ft, virtual_ms):
        end = ft.t + virtual_ms
        while ft.t < end:
            app.clock.sleep_ms(app.step())

    def payloads(self, broker):
        return [json.loads(m[1].decode()) for m in broker.messages]

    def test_publishes_once_per_second_with_expected_payload(self):
        broker = Broker()
        app, ft, logs = build_app(base_cfg(), FakeWlan(["ok"]), broker)
        self.run_app(app, ft, 5050)
        payloads = self.payloads(broker)
        self.assertEqual(len(payloads), 5)  # t = 1, 2, 3, 4, 5 s
        p = payloads[1]
        self.assertEqual(set(p), {"device_id", "sequence", "uptime_ms", "sample_count",
                                  "sensor_voltage_v", "sound_level_db_a_estimate",
                                  "sound_level_db_a_min", "sound_level_db_a_max",
                                  "out_of_range_count", "in_range"})
        self.assertEqual(p["device_id"], "pico2w-test")
        self.assertEqual(p["sequence"], 2)
        self.assertEqual(p["sample_count"], 1)  # default SAMPLE_RATE_HZ = 1
        self.assertEqual(p["out_of_range_count"], 0)
        self.assertTrue(isinstance(p["uptime_ms"], int) and 1990 <= p["uptime_ms"] <= 2010)
        self.assertAlmostEqual(p["sensor_voltage_v"], 1.2, places=3)
        self.assertAlmostEqual(p["sound_level_db_a_estimate"], 60.0, places=1)
        self.assertIs(p["in_range"], True)
        topic, _, retain, qos = broker.messages[0]
        self.assertEqual((topic, retain, qos), (b"noisenet/pico2w-test/sound", False, 0))
        self.assertEqual([q["sequence"] for q in payloads], [1, 2, 3, 4, 5])
        client_id, server, port, user, password, keepalive = broker.init_args[0]
        self.assertEqual((client_id, server, port, user, keepalive),
                         ("pico2w-test", "192.0.2.50", 1883, "noisenet", 60))
        self.assertEqual(broker.clients[0].sock.timeouts[-1], 5)

    def test_wifi_failure_then_recovery(self):
        broker = Broker()
        wlan = FakeWlan(["fail", "badpw", "ok"])
        app, ft, logs = build_app(base_cfg(), wlan, broker)
        self.run_app(app, ft, 20000)
        self.assertEqual(wlan.connect_calls, 3)
        self.assertTrue(any("timeout" in l for l in logs))
        self.assertTrue(any("authentication failed" in l for l in logs))
        self.assertTrue(broker.messages)
        self.assertEqual(len(broker.init_args), 1)  # MQTT only after Wi-Fi
        self.assertFalse(any(WIFI_PW in l or MQTT_PW in l for l in logs))

    def test_mqtt_connect_failure_retries(self):
        broker = Broker(connect_fail=2)
        app, ft, logs = build_app(base_cfg(), FakeWlan(["ok"]), broker)
        self.run_app(app, ft, 8000)
        self.assertEqual(len(broker.clients), 3)
        self.assertEqual([s.closed for s in broker.sockets], [True, True, False])
        self.assertTrue(broker.messages)

    def test_publish_failure_closes_socket_and_reconnects(self):
        broker = Broker(publish_fail_at={3})
        app, ft, logs = build_app(base_cfg(), FakeWlan(["ok"]), broker)
        self.run_app(app, ft, 10000)
        self.assertEqual(len(broker.clients), 2)
        self.assertTrue(broker.sockets[0].closed)
        self.assertIsNone(broker.clients[0].sock)  # released
        seqs = [p["sequence"] for p in self.payloads(broker)]
        self.assertNotIn(3, seqs)  # failed attempt still consumed a sequence number
        self.assertEqual(seqs[:3], [1, 2, 4])
        self.assertTrue(any("link error" in l for l in logs))

    def test_wifi_loss_reconnects_wifi_before_mqtt(self):
        broker = Broker()
        wlan = FakeWlan(["ok", "fail", "ok"])
        app, ft, logs = build_app(base_cfg(), wlan, broker)
        self.run_app(app, ft, 2500)
        wlan.connected = False  # access point disappears
        self.run_app(app, ft, 15000)
        self.assertEqual(wlan.connect_calls, 3)
        self.assertEqual(len(broker.clients), 2)
        self.assertTrue(any("link lost" in l for l in logs))
        before = len(broker.messages)
        self.run_app(app, ft, 3000)
        self.assertGreater(len(broker.messages), before)

    def test_keepalive_serviced_with_long_interval(self):
        broker = Broker()
        cfg = base_cfg(PUBLISH_INTERVAL_S=120, MQTT_KEEPALIVE_S=20)
        app, ft, logs = build_app(cfg, FakeWlan(["ok"]), broker)
        self.run_app(app, ft, 119000)
        self.assertEqual(len(broker.messages), 0)  # first reading at 120 s
        self.assertGreaterEqual(broker.pings, 10)  # every 10 s (keepalive / 2)

    def test_legacy_umqtt_without_connect_timeout(self):
        broker = Broker(legacy=True)
        app, ft, logs = build_app(base_cfg(), FakeWlan(["ok"]), broker)
        self.run_app(app, ft, 1500)
        self.assertTrue(broker.messages)
        self.assertTrue(any("no connect timeout" in l for l in logs))

    def test_open_network_password_not_passed(self):
        wlan = FakeWlan(["ok"])
        app, ft, logs = build_app(base_cfg(WIFI_PASSWORD=""), wlan, Broker())
        app.step()
        self.assertEqual(wlan.passwords_seen, [None])

    def test_run_closes_socket_on_exit(self):
        broker = Broker()
        app, ft, logs = build_app(base_cfg(), FakeWlan(["ok"]), broker)
        app.run(max_steps=5)
        self.assertIsNone(broker.clients[0].sock)


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.saved = {k: sys.modules.get(k) for k in ("secrets", "umqtt", "umqtt.simple")}

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def run_main(self):
        out = io.StringIO()
        with redirect_stdout(out):
            main.main()
        return out.getvalue()

    def run_main_sensor_only(self, module):
        sys.modules["secrets"] = module
        sys.modules["umqtt"] = None  # sensor-only mode must not need umqtt
        sys.modules["umqtt.simple"] = None
        ran = []
        saved = main.SEN0232, main.SensorOnlyApp, main.Clock
        main.SEN0232 = lambda *a: ("sensor", a)
        main.Clock = lambda: "clock"
        main.SensorOnlyApp = lambda cfg, sensor, clock, **kw: types.SimpleNamespace(
            run=lambda: ran.append((cfg["WIFI_SSID"], sensor)))
        try:
            out = self.run_main()
        finally:
            main.SEN0232, main.SensorOnlyApp, main.Clock = saved
        return out, ran

    def test_without_secrets_runs_sensor_only(self):
        out, ran = self.run_main_sensor_only(types.ModuleType("secrets"))
        self.assertIn("sensor-only mode", out)
        self.assertEqual(ran, [(None, ("sensor", (26, 3.3, 1.0, 0.0)))])

    def test_ssid_none_runs_sensor_only_and_ignores_network_settings(self):
        mod = types.ModuleType("secrets")
        mod.WIFI_SSID = None
        mod.MQTT_BROKER = "<broker-ip-or-hostname>"  # ignored without Wi-Fi
        mod.MQTT_PASSWORD = MQTT_PW
        mod.ADC_GPIO = 27
        out, ran = self.run_main_sensor_only(mod)
        self.assertIn("Wi-Fi: disabled", out)
        self.assertNotIn("invalid configuration", out)
        self.assertNotIn(MQTT_PW, out)
        self.assertEqual(ran[0][1][1][0], 27)

    def test_reports_invalid_config_without_secrets(self):
        mod = types.ModuleType("secrets")
        mod.WIFI_SSID = "<your-ssid>"
        mod.WIFI_PASSWORD = WIFI_PW
        sys.modules["secrets"] = mod
        out = self.run_main()
        self.assertIn("invalid configuration", out)
        self.assertNotIn(WIFI_PW, out)

    def test_reports_missing_umqtt(self):
        mod = types.ModuleType("secrets")
        for k, v in base_cfg().items():
            setattr(mod, k, v)
        sys.modules["secrets"] = mod
        sys.modules["umqtt"] = None  # makes "import umqtt.simple" fail
        sys.modules["umqtt.simple"] = None
        out = self.run_main()
        self.assertIn("umqtt.simple is not installed", out)
        self.assertNotIn(MQTT_PW, out)


if __name__ == "__main__":
    unittest.main()
