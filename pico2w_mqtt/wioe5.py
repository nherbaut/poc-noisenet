"""LoRaWAN uplink of per-slot maximum levels through a Seeed Wio-E5.

The Wio-E5 runs the LoRaWAN stack and is driven with AT commands over UART
(9600 baud). This module contains:

- pure helpers: batch payload encoding/decoding, LoRa time-on-air estimate;
- AtModem: line-oriented, non-blocking AT transport over a UART object;
- LoraUplink: OTAA join state machine and the periodic batched uplink.

Time is divided into slots of LORA_SLOT_S seconds. The maximum sample level of
each slot is kept; every LORA_SLOTS_PER_UPLINK slots, one uplink carries them
all (default: one maximum per minute, one uplink every 5 minutes).

Payload format version 1 (big-endian), LoRaWAN FPort LORA_PORT, 2N + 5 bytes:

    byte 0        bits 7-4 format version (1), bits 3-0 slot count N (1-8)
    byte 1        slot length in units of 10 s
    byte 2        sequence number modulo 256
    bytes 3..     N x uint16: slot maximum x10 (dBA estimate), oldest first;
                  0xFFFF = no sample in the slot
    byte 3+2N     bit i: slot i contained an out-of-range sample
    byte 4+2N     bit i: slot i had sample gaps (< 90 % of expected) or none

The last slot ends just before transmission. The maximum is the highest
per-sample estimate of a slot. It is not a standards-compliant LAFmax; its
value depends on SAMPLE_RATE_HZ.

Boot message format version 1, FPort LORA_BOOT_PORT, 10 bytes, sent once per
start-up right after the join (or "Joined already"), never retried:

    byte 0     message format version (1)
    bytes 1-2  firmware version major, minor
    byte 3     reset cause: 0 unknown, 1 power-on, 2 watchdog or machine.reset(),
               3 other
    byte 4     slot length in units of 10 s
    byte 5     slots per uplink
    bytes 6-7  uint16 sample rate x100 (Hz)
    byte 8     flags: bit0 Wi-Fi enabled, bit1 calibration applied, bit2 ADR on
    byte 9     configured data rate (0-5), 0xFF = module setting kept
"""

from sen0232 import LevelAggregator

FORMAT_VERSION = 1
MAX_SLOTS = 8
NO_LEVEL = 0xFFFF

# EU868 data rates on 125 kHz (Wio-E5 AT specification, table 3-1).
EU868_DR_SF = {0: 12, 1: 11, 2: 10, 3: 9, 4: 8, 5: 7}
LORAWAN_OVERHEAD_BYTES = 13  # MHDR + FHDR (no FOpts) + FPort + MIC
TTN_FAIR_USE_S_PER_DAY = 30

# Valid RP2350 UART pin pairs for the standard UART function.
UART_TX_PINS = {0: (0, 12, 16, 28), 1: (4, 8, 20, 24)}
UART_RX_PINS = {0: (1, 13, 17, 29), 1: (5, 9, 21, 25)}


BOOT_FORMAT_VERSION = 1
BOOT_LENGTH = 10
RESET_UNKNOWN, RESET_POWER_ON, RESET_WATCHDOG, RESET_OTHER = 0, 1, 2, 3
RESET_NAMES = {0: "unknown", 1: "power-on", 2: "watchdog or machine.reset()", 3: "other"}
BOOT_FLAG_WIFI = 0x01
BOOT_FLAG_CALIBRATED = 0x02
BOOT_FLAG_ADR = 0x04


def reset_cause_code():
    """Map machine.reset_cause() to a boot-message code (0 when unavailable)."""
    try:
        import machine

        cause = machine.reset_cause()
    except Exception:
        return RESET_UNKNOWN
    if cause == getattr(machine, "PWRON_RESET", None):
        return RESET_POWER_ON
    if cause == getattr(machine, "WDT_RESET", None):
        return RESET_WATCHDOG
    return RESET_OTHER


def encode_boot(version, reset_cause, slot_s, slots, sample_rate_hz, flags, data_rate):
    rate = int(sample_rate_hz * 100 + 0.5)
    if rate > 0xFFFF:
        rate = 0xFFFF
    return bytes((
        BOOT_FORMAT_VERSION,
        version[0] & 0xFF,
        version[1] & 0xFF,
        reset_cause & 0xFF,
        int(slot_s // 10) & 0xFF,
        slots & 0xFF,
        rate >> 8,
        rate & 0xFF,
        flags & 0xFF,
        0xFF if data_rate is None else data_rate & 0xFF,
    ))


def decode_boot(data):
    """Reference decoder (host tests); mirrors the TTN payload formatter."""
    if len(data) != BOOT_LENGTH or data[0] != BOOT_FORMAT_VERSION:
        raise ValueError("unknown boot message format")
    return {
        "message": "boot",
        "firmware_version": "%d.%d" % (data[1], data[2]),
        "reset_cause": RESET_NAMES.get(data[3], "code %d" % data[3]),
        "slot_s": data[4] * 10,
        "slots_per_uplink": data[5],
        "sample_rate_hz": ((data[6] << 8) | data[7]) / 100.0,
        "wifi_enabled": bool(data[8] & BOOT_FLAG_WIFI),
        "calibration_applied": bool(data[8] & BOOT_FLAG_CALIBRATED),
        "adr": bool(data[8] & BOOT_FLAG_ADR),
        "data_rate": None if data[9] == 0xFF else data[9],
    }


def payload_length(slots):
    return 2 * slots + 5


def _level_x10(summary):
    if summary is None or summary["count"] == 0:
        return NO_LEVEL
    value = int(summary["level_max_db_a"] * 10 + 0.5)
    if value < 0:
        return 0
    if value > NO_LEVEL - 1:
        return NO_LEVEL - 1
    return value


def encode_batch(summaries, sequence, slot_s, expected_per_slot):
    """Encode slot summaries (LevelAggregator.summary() dicts or None)."""
    n = len(summaries)
    if not 1 <= n <= MAX_SLOTS:
        raise ValueError("slot count must be 1..%d" % MAX_SLOTS)
    out = bytearray(payload_length(n))
    out[0] = (FORMAT_VERSION << 4) | n
    out[1] = int(slot_s // 10)
    out[2] = sequence & 0xFF
    out_of_range = 0
    gaps = 0
    for i, summary in enumerate(summaries):
        value = _level_x10(summary)
        out[3 + 2 * i] = value >> 8
        out[4 + 2 * i] = value & 0xFF
        if summary is None or summary["count"] == 0:
            gaps |= 1 << i
            continue
        if summary["out_of_range"]:
            out_of_range |= 1 << i
        if summary["count"] * 10 < expected_per_slot * 9:
            gaps |= 1 << i
    out[3 + 2 * n] = out_of_range
    out[4 + 2 * n] = gaps
    return bytes(out)


def decode_batch(data):
    """Reference decoder (host tests); mirrors the TTN payload formatter."""
    if len(data) < 5 or data[0] >> 4 != FORMAT_VERSION:
        raise ValueError("unknown payload format")
    n = data[0] & 0x0F
    if len(data) != payload_length(n):
        raise ValueError("length mismatch")
    slot_s = data[1] * 10
    out_of_range = data[3 + 2 * n]
    gaps = data[4 + 2 * n]
    slots = []
    for i in range(n):
        raw = (data[3 + 2 * i] << 8) | data[4 + 2 * i]
        slots.append({
            "end_offset_s": -(n - 1 - i) * slot_s,
            "max_db_a_estimate": None if raw == NO_LEVEL else raw / 10.0,
            "out_of_range": bool((out_of_range >> i) & 1),
            "sample_gaps": bool((gaps >> i) & 1),
        })
    return {"message": "slots", "sequence": data[2], "slot_s": slot_s, "slots": slots}


def airtime_ms(spreading_factor, payload_bytes, bandwidth_hz=125000, preamble=8):
    """LoRa time on air (explicit header, CRC on, coding rate 4/5)."""
    sf = spreading_factor
    t_sym_ms = (1 << sf) * 1000.0 / bandwidth_hz
    de = 1 if (sf >= 11 and bandwidth_hz == 125000) else 0
    num = 8 * payload_bytes - 4 * sf + 28 + 16
    den = 4 * (sf - 2 * de)
    n = -(-num // den) if num > 0 else 0  # ceiling division
    symbols = 8 + n * 5
    return (preamble + 4.25 + symbols) * t_sym_ms


def daily_airtime_s(data_rate, interval_s, app_payload):
    sf = EU868_DR_SF.get(data_rate, 12)
    per_msg_ms = airtime_ms(sf, app_payload + LORAWAN_OVERHEAD_BYTES)
    return per_msg_ms * (86400.0 / interval_s) / 1000.0


class AtModem:
    """Line-oriented AT transport. Never logs lines marked as secret."""

    def __init__(self, uart, clock, logger):
        self.uart = uart
        self.clock = clock
        self.log = logger
        self._buf = b""

    def _read_available(self):
        n = self.uart.any()
        if n:
            data = self.uart.read(n)
            if data:
                self._buf += data

    def lines(self):
        """Return complete lines received so far (decoded, stripped)."""
        self._read_available()
        out = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            try:
                text = line.decode().strip()
            except UnicodeError:
                continue  # line garbled on the serial link
            if text:
                out.append(text)
        return out

    def flush(self):
        self._read_available()
        self._buf = b""

    def write(self, command):
        self.uart.write(command.encode() + b"\r\n")

    def command(self, command, prefix, timeout_ms=2000):
        """Blocking request/response, used only during start-up configuration.

        Returns the first line starting with prefix, or None on timeout.
        """
        self.flush()
        self.write(command)
        deadline = self.clock.now_ms() + timeout_ms
        while self.clock.now_ms() < deadline:
            for line in self.lines():
                if line.startswith(prefix):
                    return line
            self.clock.sleep_ms(20)
        return None


def make_uart(cfg):
    from machine import UART, Pin

    return UART(
        cfg["LORA_UART_ID"],
        baudrate=cfg["LORA_BAUD"],
        tx=Pin(cfg["LORA_TX_GPIO"]),
        rx=Pin(cfg["LORA_RX_GPIO"]),
        timeout=0,
    )


class LoraUplink:
    UNCONFIGURED = "unconfigured"  # Wio-E5 not answering yet
    UNJOINED = "unjoined"
    JOINING = "joining"
    JOINED = "joined"
    SENDING = "sending"

    STARTUP_PROBE_MS = 10000  # the module may still be booting after power-up
    PROBE_INTERVAL_MS = 500
    RECONFIGURE_PROBE_MS = 1000  # short probe for periodic retries (sampling pauses)
    CONFIG_RETRY_MS = 60000
    JOIN_TIMEOUT_MS = 30000
    SEND_TIMEOUT_MS = 20000
    SEND_ERRORS = ("busy", "join network first", "No free channel", "No band",
                   "DR error", "Length error", "ERROR")

    def __init__(self, cfg, modem, clock, logger, boot_payload=None):
        self.modem = modem
        self.boot_payload = boot_payload  # sent once after the first join
        self.port = cfg["LORA_PORT"]
        self.boot_port = cfg["LORA_BOOT_PORT"]
        self.current_port = None
        self.clock = clock
        self.log = logger
        self.cfg = cfg
        self.slot_s = cfg["LORA_SLOT_S"]
        self.slots_per_uplink = cfg["LORA_SLOTS_PER_UPLINK"]
        self.interval_ms = int(self.slot_s * 1000)  # one slot
        self.join_retry_ms = int(cfg["LORA_JOIN_RETRY_S"] * 1000)
        self.expected_samples = cfg["SAMPLE_RATE_HZ"] * self.slot_s
        self.slots = []  # summaries of the completed slots of the current batch
        self.state = self.UNCONFIGURED
        self.next_config_ms = 0
        self.window = LevelAggregator()
        self.sequence = 0
        now = clock.now_ms()
        self.window_end_ms = now + self.interval_ms
        self.next_join_ms = now
        self.deadline_ms = 0
        self._joined_seen = False
        self._pending = None  # (name, text) of the uplink in flight
        self.op_started_ms = now

    # -- start-up -----------------------------------------------------------

    def _probe(self, total_ms):
        """Send AT until the module answers or total_ms elapses."""
        deadline = self.clock.now_ms() + total_ms
        attempts = 0
        while True:
            attempts += 1
            if self.modem.command("AT", "+AT", timeout_ms=self.PROBE_INTERVAL_MS) is not None:
                return attempts
            if self.clock.now_ms() >= deadline:
                return 0

    def configure(self, probe_ms=None):
        """Blocking configuration. Returns True on success.

        After power-up the Wio-E5 needs time to boot; "AT" is repeated for up
        to probe_ms (default STARTUP_PROBE_MS) before giving up.
        """
        m = self.modem
        attempts = self._probe(self.STARTUP_PROBE_MS if probe_ms is None else probe_ms)
        if not attempts:
            self.log("LoRa: no reply from Wio-E5 on UART%d (TX GP%d, RX GP%d)"
                     % (self.cfg["LORA_UART_ID"], self.cfg["LORA_TX_GPIO"],
                        self.cfg["LORA_RX_GPIO"]))
            return False
        if attempts > 1:
            self.log("LoRa: Wio-E5 answered after %d attempts" % attempts)
        steps = [
            ("AT+MODE=LWOTAA", "+MODE"),
            ("AT+DR=EU868", "+DR"),
            ("AT+CH=NUM,0-2", "+CH"),
            ("AT+PORT=%d" % self.cfg["LORA_PORT"], "+PORT"),
            ("AT+ADR=%s" % ("ON" if self.cfg["LORA_ADR"] else "OFF"), "+ADR"),
        ]
        if self.cfg["LORA_DATA_RATE"] is not None:
            steps.append(("AT+DR=%d" % self.cfg["LORA_DATA_RATE"], "+DR"))
        if self.cfg["LORA_JOIN_EUI"]:
            steps.append(('AT+ID=AppEui,"%s"' % self.cfg["LORA_JOIN_EUI"], "+ID"))
        for command, prefix in steps:
            reply = m.command(command, prefix)
            if reply is None or "ERROR" in reply:
                self.log("LoRa: configuration failed at %s (%s)" % (command, reply))
                return False
        if self.cfg["LORA_APP_KEY"]:
            # The reply echoes the key: check it, never log it.
            reply = m.command('AT+KEY=APPKEY,"%s"' % self.cfg["LORA_APP_KEY"], "+KEY")
            if reply is None or "ERROR" in reply:
                self.log("LoRa: AppKey programming failed")
                return False
        self.current_port = self.cfg["LORA_PORT"]
        self.log("LoRa: Wio-E5 configured (EU868, OTAA, port %d, %s)"
                 % (self.cfg["LORA_PORT"],
                    "ADR on" if self.cfg["LORA_ADR"] else
                    "DR%s, ADR off" % self.cfg["LORA_DATA_RATE"]))
        return True

    # -- sampling -----------------------------------------------------------

    def add(self, raw, voltage_v, level_db_a, in_range):
        self.window.add(raw, voltage_v, level_db_a, in_range)

    # -- main-loop servicing --------------------------------------------------

    def start(self):
        """Initial configuration at start-up; never disables the uplink."""
        if self.configure():
            self.state = self.UNJOINED
            self.next_join_ms = self.clock.now_ms()
        else:
            self.next_config_ms = self.clock.now_ms() + self.CONFIG_RETRY_MS
            self.log("LoRa: retrying the Wio-E5 configuration every %d s"
                     % (self.CONFIG_RETRY_MS // 1000))

    def ms_until_next(self, now_ms):
        nxt = self.window_end_ms
        if self.state == self.UNJOINED:
            nxt = min(nxt, self.next_join_ms)
        elif self.state == self.UNCONFIGURED:
            nxt = min(nxt, self.next_config_ms)
        return nxt - now_ms

    def _start_join(self, now):
        self.log("LoRa: joining (OTAA)")
        self.modem.flush()
        self.modem.write("AT+JOIN")
        self.state = self.JOINING
        self._joined_seen = False
        self.op_started_ms = now
        self.deadline_ms = now + self.JOIN_TIMEOUT_MS

    def _join_finished(self, now, joined, reason=""):
        if joined:
            self.state = self.JOINED
            self.log("LoRa: network joined")
        else:
            self.state = self.UNJOINED
            self.next_join_ms = now + self.join_retry_ms
            self.log("LoRa: join failed after %.1f s%s; retrying in %d s"
                     % ((now - self.op_started_ms) / 1000.0,
                        " (%s)" % reason if reason else "", self.join_retry_ms // 1000))

    def _handle_line(self, now, line):
        if self.state in (self.JOINING, self.SENDING):
            # Raw module reply, with time since AT+JOIN / AT+MSGHEX. These replies
            # never contain keys (AT+KEY is only sent during configure()).
            self.log("LoRa: Wio-E5 [t+%.1f s] %s" % ((now - self.op_started_ms) / 1000.0, line))
        if self.state == self.JOINING and line.startswith("+JOIN:"):
            if "NetID" in line or "Network joined" in line or "Joined already" in line:
                self._joined_seen = True
                if "Joined already" in line:
                    self._join_finished(now, True)
            elif "busy" in line:
                self._join_finished(now, False, "modem busy")
            elif line.startswith("+JOIN: Done"):
                self._join_finished(now, self._joined_seen)
        elif self.state == self.SENDING and line.startswith("+MSGHEX:"):
            name, text = self._pending
            if line.startswith("+MSGHEX: Done"):
                self.state = self.JOINED
                self.log("LoRa: %s sent (%s)" % (name, text))
            else:
                for err in self.SEND_ERRORS:
                    if err in line:
                        self.log("LoRa: %s not sent (%s)" % (name, line))
                        if "join network first" in line:
                            self.state = self.UNJOINED
                            self.next_join_ms = now
                        else:
                            self.state = self.JOINED
                        break

    def _close_window(self, now):
        summary = self.window.summary() if self.window.count else None
        self.window.reset()
        self.window_end_ms += self.interval_ms
        if self.window_end_ms <= now:
            self.window_end_ms = now + self.interval_ms
        self.slots.append(summary)
        self.log("LoRa: slot %d/%d max %s" % (
            len(self.slots), self.slots_per_uplink,
            "%.1f dBA estimate, n=%d" % (summary["level_max_db_a"], summary["count"])
            if summary else "none (no samples)"))
        if len(self.slots) < self.slots_per_uplink:
            return
        slots, self.slots = self.slots, []
        if self.state != self.JOINED:
            self.log("LoRa: batch of %d slots not sent (state: %s)"
                     % (len(slots), self.state))
            return
        self.sequence += 1
        payload = encode_batch(slots, self.sequence, self.slot_s, self.expected_samples)
        levels = [s["level_max_db_a"] for s in slots if s]
        self._send(now, self.port, payload, "uplink #%d" % self.sequence,
                   "%d slots, highest %s" % (
                       len(slots), "%.1f dBA estimate" % max(levels) if levels else "none"))

    def _send(self, now, port, payload, name, text):
        """Start an unconfirmed uplink on port. Returns False if not started."""
        if self.current_port != port:
            # Short blocking exchange (one AT reply); the module is idle here.
            reply = self.modem.command("AT+PORT=%d" % port, "+PORT")
            if reply is None or "ERROR" in reply:
                self.log("LoRa: %s not sent (cannot select port %d)" % (name, port))
                self.current_port = None
                return False
            self.current_port = port
        hexstr = "".join("%02X" % b for b in payload)
        self.modem.flush()
        self.modem.write('AT+MSGHEX="%s"' % hexstr)
        self.state = self.SENDING
        self.op_started_ms = now
        self.deadline_ms = now + self.SEND_TIMEOUT_MS
        self._pending = (name, text)
        return True

    def _send_boot(self, now):
        payload, self.boot_payload = self.boot_payload, None  # never retried
        info = decode_boot(payload)
        self._send(now, self.boot_port, payload, "boot message",
                   "firmware %s, reset %s, port %d"
                   % (info["firmware_version"], info["reset_cause"], self.boot_port))

    def poll(self):
        now = self.clock.now_ms()
        for line in self.modem.lines():
            self._handle_line(now, line)
        if self.state in (self.JOINING, self.SENDING) and now >= self.deadline_ms:
            if self.state == self.JOINING:
                self._join_finished(now, False, "timeout")
            else:
                self.log("LoRa: %s: no confirmation from Wio-E5" % self._pending[0])
                self.state = self.JOINED
        if self.state == self.UNCONFIGURED and now >= self.next_config_ms:
            if self.configure(probe_ms=self.RECONFIGURE_PROBE_MS):
                self.state = self.UNJOINED
                self.next_join_ms = now
            else:
                self.next_config_ms = self.clock.now_ms() + self.CONFIG_RETRY_MS
            now = self.clock.now_ms()
        if self.state == self.UNJOINED and now >= self.next_join_ms:
            self._start_join(now)
        if self.state == self.JOINED and self.boot_payload is not None:
            self._send_boot(now)
        if now >= self.window_end_ms:
            self._close_window(now)
