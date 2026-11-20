# Pico 2 W: SEN0232 sound readings over Wi-Fi and MQTT

Standalone MicroPython bench prototype. A Raspberry Pi Pico 2 W reads a DFRobot
SEN0232 sound-level sensor and publishes one JSON reading per second (configurable)
to an MQTT broker on the local network.

Scope and limits:

- This example does not replace the NoiseNet station design (LoRaWAN EU868 via
  Wio-E5, one-minute aggregates, compact binary payloads; see `src/README.md` and
  `AGENTS.md`). GNSS, BME280, LoRaWAN and battery telemetry are not implemented here.
- MQTT runs over plain TCP (default port 1883) without TLS. It is intended only for
  a trusted local network. Credentials and payloads are transmitted unencrypted.
- The published value is an **estimated A-weighted sensor sound level**: the energy
  average of the SEN0232 samples taken at `SAMPLE_RATE_HZ` during the reading
  interval (one sample per reading by default). It is not a standards-compliant LAeq,
  LAFmax or minute aggregate, and no field accuracy is claimed. The default calibration (`CAL_A = 1.0`, `CAL_B = 0.0`) applies no
  correction: the assembled system is uncalibrated.
- Wi-Fi is optional. With `WIFI_SSID = None` (the template default), or without
  `secrets.py`, the application runs in **sensor-only mode**: it prints one reading
  per interval on the USB console, uses no network and does not need `umqtt.simple`.
- QoS 0 is used with retain disabled. Messages may be lost; no offline backlog is
  stored. Readings taken while disconnected are not produced.

## Files

| File | Role |
|---|---|
| `main.py` | Startup, configuration validation, Wi-Fi, MQTT, publication and reconnection loop |
| `sen0232.py` | ADC access (isolated in class `SEN0232`) and pure voltage-to-level conversion |
| `wioe5.py` | Wio-E5 AT driver, OTAA join, batched per-slot maximum uplink over LoRaWAN |
| `ttn_formatter.js` | TTN uplink payload formatter for measurement and boot messages (server side, not deployed to the Pico) |
| `config.example.py` | Configuration template; copy to `secrets.py` |
| `Makefile` | Deployment over USB serial with `mpremote`, REPL access, host checks |
| `tools/config_check.py` | Host-side validation of `secrets.py` (used by the Makefile) |
| `tests/test_host.py` | Host-side checks with fakes (CPython, no hardware) |

`secrets.py` is excluded from git by the repository rule `**/secrets.*`. Never
commit real Wi-Fi or MQTT credentials.

## Quick start with `make`

Requirements on the computer: Python 3, GNU make and
[`mpremote`](https://docs.micropython.org/en/latest/reference/mpremote.html)
(`make tools` installs it with `pipx`, or `pip --user` as a fallback). MicroPython must
already be installed on the Pico (setup step 1). Close Thonny or any other serial
terminal first: only one program can open the serial port.

```sh
make deploy     # creates secrets.py from the template if absent (sensor-only),
                # validates it, copies the files over USB serial, resets the Pico
make debug      # MicroPython REPL/console of the Pico
```

| Target | Action |
|---|---|
| `make help` | List targets and variables |
| `make tools` | Install `mpremote` on the computer |
| `make check` | Compile sources and validate `secrets.py` on the computer |
| `make test` | Host-side unit tests (no hardware) |
| `make deploy` | Copy `secrets.py`, `sen0232.py`, `wioe5.py`, `main.py` to the Pico root and hard-reset it. When `WIFI_SSID` is set, first install `umqtt.simple` (downloaded by the computer; the Pico needs no Internet access) |
| `make umqtt` | Install or update `umqtt.simple` only |
| `make run` | Run the computer's `main.py` on the Pico without saving it (output in the terminal) |
| `make debug` | Open the REPL. The running `main.py` output is shown; Ctrl-C interrupts it and gives the `>>>` prompt; Ctrl-D soft-reboots and restarts `main.py`; Ctrl-] or Ctrl-X quits `mpremote` |
| `make reset` | Hard-reset the Pico (restarts `main.py`) |
| `make ls` | List files on the Pico |

Variables: `PORT` (default `auto`, the first USB serial MicroPython device; e.g.
`make deploy PORT=/dev/ttyACM0`), `UMQTT=0` (skip the `umqtt.simple` installation),
`MPREMOTE`, `PYTHON`.

After `make deploy`, the USB serial port disappears for about one second during the
reset; wait briefly before `make debug`. On Linux, serial access requires membership
of the `dialout` group (`sudo usermod -aG dialout $USER`, then log in again).

Sensor-only console output:

```text
noisenet-pico2w-mqtt starting
Wi-Fi: disabled (WIFI_SSID = None); sensor-only mode, readings are shown on the USB console only
ADC: GP26, reference 3.300 V
...
#1 n=1 raw=23831 1.200 V 60.0 dBA (estimate)
#2 n=1 raw=24102 1.214 V 60.7 dBA (estimate)
```

With `SAMPLE_RATE_HZ = 10`, each line also shows the extreme sample levels; `raw` is
the last sample of the interval:

```text
#3 n=10 raw=24310 1.203 V 60.4 dBA (estimate) min 59.1 max 62.0
```

## Wiring

Disconnect USB power before changing wiring.

| SEN0232 | Pico 2 W |
|---|---|
| `+` / VCC | `3V3(OUT)` (pin 36) |
| `-` / GND | GND (e.g. pin 38) |
| `A` / analog output | GP26 / ADC0 (pin 31) |

`ADC_GPIO` accepts 26, 27 or 28 (ADC0–ADC2). GP27 is the documented alternative if
GP26 is needed elsewhere. Never feed 5 V into `3V3`.

## Measurement

`machine.ADC.read_u16()` returns 0–65535. The firmware computes:

```text
voltage_v          = raw * ADC_REFERENCE_V / 65535
level_db_a         = voltage_v * 50                  (DFRobot SEN0232 conversion)
level_db_a_output  = CAL_A * level_db_a + CAL_B      (per-device correction)
in_range           = 0.6 V <= voltage_v <= 2.6 V     (30–130 dBA)
```

`ADC_REFERENCE_V` defaults to the nominal 3.3 V. The ADC reference on the Pico 2 W
is derived from the 3.3 V rail, so any deviation of that rail scales every voltage
and therefore every level proportionally (1 % reference error gives 1 % level error,
about 0.6 dB at 60 dBA). For better accuracy, measure `3V3` (or `ADC_VREF`, pin 35)
with a multimeter under load and set `ADC_REFERENCE_V` to the measured value.

### Sampling rate

`SAMPLE_RATE_HZ` (default 1.0, range 0.01–200 Hz) sets how often the ADC is read.
`PUBLISH_INTERVAL_S` sets how often a reading is produced. Each reading summarizes
the samples taken during its interval:

```text
level_db_a  = 10 * log10( mean( 10^(L_i / 10) ) )     energy average of the samples
min, max    = extreme sample levels in the interval
sample_count, out_of_range_count
sensor_voltage_v = arithmetic mean of the sample voltages
```

`SAMPLE_RATE_HZ * PUBLISH_INTERVAL_S` must be at least 1. The first reading is
produced one interval after start (sensor-only) or after each MQTT connection; samples
taken while disconnected are discarded.

Sampling is driven cooperatively by the main loop, not by a hardware timer. While the
loop is blocked (Wi-Fi association, MQTT connection, socket timeout, retry delay),
no samples are taken and late slots are skipped rather than sampled in a burst. A
`sample_count` lower than `SAMPLE_RATE_HZ * PUBLISH_INTERVAL_S` indicates such gaps.
Sample timing jitter is in the millisecond range; the rate is therefore suitable for
level averaging, not for acoustic waveform capture. The SEN0232 output is already a
level signal: sampling faster does not add frequency information.

Voltages outside 0.6–2.6 V are published unchanged and counted in
`out_of_range_count`; `"in_range"` is `false` when at least one sample of the interval
was out of range. The console marks such readings `OUT OF RANGE`. They are not clamped. Calibration coefficients
determined against a reference sound-level meter on the assembled station belong in
`CAL_A`/`CAL_B` (see `docs/calibration.md`).

## Payload

Topic: `MQTT_TOPIC`, default `noisenet/<device_id>/sound`. `device_id` is
`DEVICE_ID`, or `pico2w-<hex of machine.unique_id()>` when unset.

```json
{
  "device_id": "pico2w-example",
  "sequence": 1,
  "uptime_ms": 12345,
  "sample_count": 10,
  "sensor_voltage_v": 1.2,
  "sound_level_db_a_estimate": 60.0,
  "sound_level_db_a_min": 58.7,
  "sound_level_db_a_max": 61.4,
  "out_of_range_count": 0,
  "in_range": true
}
```

- `sequence` increments on every publication attempt, including failed attempts,
  and restarts at 1 after reboot. Gaps therefore indicate lost or failed messages.
- `uptime_ms` is monotonic elapsed time since application start. It is accumulated
  from `time.ticks_diff()` steps, so it does not wrap with `time.ticks_ms()`.
  No wall-clock time or NTP is used.
- `sensor_voltage_v` (mean of the samples) is rounded to 1 mV; the level fields to
  0.1 dB. With one sample per reading, estimate, min and max are equal.

## Setup

### 1. Install MicroPython for Pico 2 W

Download the latest **stable** `.uf2` from
<https://micropython.org/download/RPI_PICO2_W/> (not the `RPI_PICO2` build, which
lacks Wi-Fi, and not a "preview" build). Hold BOOTSEL while connecting USB, then copy
the `.uf2` to the `RP2350` drive. The board reboots into MicroPython. Record the
version printed by the REPL banner in the test log.

Thonny (Tools → Options → Interpreter → "Install or update MicroPython") can perform
the same installation; select family RP2 and variant Raspberry Pi Pico 2 W.

Use a USB **data** cable (the Multicomp MC002726 in the BOM). A power-only cable
powers the board but no serial port or drive appears.

### 2. Wire the sensor

Follow the wiring table above with USB disconnected.

### 3. Create `secrets.py`

On the computer, in this directory:

```sh
cp config.example.py secrets.py
```

`make deploy` performs this copy automatically when `secrets.py` is absent. The
template sets `WIFI_SSID = None` (sensor-only mode); Wi-Fi and MQTT settings are then
ignored and not validated. To enable Wi-Fi/MQTT, edit `secrets.py` and replace every
`<...>` placeholder:

```python
WIFI_SSID = "<your-ssid>"
WIFI_PASSWORD = "<your-wifi-password>"
MQTT_BROKER = "<broker-ip-or-hostname>"
MQTT_USER = None        # or "<mqtt-user>" when the broker requires authentication
MQTT_PASSWORD = None    # or "<mqtt-password>"
```

Requirements:

- The Pico 2 W supports **2.4 GHz** Wi-Fi only.
- `MQTT_BROKER` must be reachable from the Pico's Wi-Fi network, for example the
  LAN address of the computer running Mosquitto. `localhost` or `127.0.0.1` would
  designate the Pico itself and is rejected by validation.
- Prefer an IP address: DNS resolution in `umqtt.simple` has no timeout.
- The Mosquitto 2.x default listener accepts only local connections. To accept LAN
  clients, the broker needs an explicit listener, for example in `mosquitto.conf`:

  ```text
  listener 1883 0.0.0.0
  allow_anonymous false
  password_file /etc/mosquitto/passwd
  ```

  Create users with `mosquitto_passwd -c /etc/mosquitto/passwd <mqtt-user>`. For a
  first isolated bench test, `allow_anonymous true` is possible on a trusted network.
  Allow TCP 1883 in the host firewall.

### 4. Copy files and install `umqtt.simple`

The Pico must contain, at the root of its filesystem:

```text
/main.py
/sen0232.py
/secrets.py
/lib/umqtt/simple.py   (or simple.mpy, installed by mip)
```

`pip` on the computer does **not** install libraries onto the Pico. Use one of the
following methods.

**A. Thonny file transfer.** Open View → Files. In the computer pane, select
`main.py`, `sen0232.py` and `secrets.py`, right-click → "Upload to /". Do not
upload `config.example.py` or `tests/`.

**B. `mip` on the Pico (needs Internet access through Wi-Fi).** After uploading
`secrets.py`, stop any running program (Ctrl-C or Thonny's Stop button) and type in
the REPL:

```python
import network, time, secrets
wlan = network.WLAN(network.STA_IF)
wlan.active(True)
wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)
for _ in range(30):
    if wlan.isconnected():
        break
    time.sleep(1)
print(wlan.isconnected(), wlan.ifconfig()[0])
import mip
mip.install("umqtt.simple")
```

This reads the credentials from `secrets.py`, so they do not appear in REPL
history. `mip` installs into `/lib/umqtt/`.

**C. `mpremote` from the computer (Pico itself needs no Internet access).**

```sh
pip install --user mpremote          # installs the tool on the computer
mpremote connect auto mip install umqtt.simple
mpremote connect auto cp main.py sen0232.py secrets.py :
```

Close Thonny first; only one program can use the serial port.

**D. Manual copy (offline).** Download `umqtt/simple.py` from
<https://github.com/micropython/micropython-lib/tree/master/micropython/umqtt.simple>,
create the folder `/lib/umqtt/` on the Pico (Thonny: right-click → New directory)
and upload `simple.py` into it.

API verification: the application uses `MQTTClient(client_id, server, port, user,
password, keepalive)`, `connect(timeout=...)`, `publish(topic, msg, retain, qos)`,
`ping()`, `check_msg()` and `set_callback()`. `connect(clean_session=True,
timeout=None)` is present in the current micropython-lib `umqtt.simple`. If an older
copy without the `timeout` argument is installed, the application falls back to
`connect()` and prints a warning; in that case the initial TCP connection attempt is
not bounded. Reinstall `umqtt.simple` to remove the warning.

### 5. Run

First test: open `main.py` in Thonny (from the computer or the Pico) and press Run
(F5). The Shell shows the configuration (passwords shown only as `<set>`/`<not set>`),
connection status and one line per reading:

```text
noisenet-pico2w-mqtt starting
Wi-Fi SSID: bench-net (password <set>)
MQTT broker: 192.0.2.50:1883 (plain TCP, no TLS)
...
Wi-Fi: connected, address 192.0.2.10
MQTT: connected
#1 n=1 raw=23831 1.200 V 60.0 dBA (estimate)
```

Automatic start: MicroPython runs `/main.py` at every boot or reset. Once `main.py`
is saved on the Pico, unplugging and reconnecting power, or pressing Ctrl-D in the
REPL (soft reset), starts the application.

Interrupt: press Ctrl-C in the Thonny Shell (or Thonny's Stop button, or in
`mpremote`). The loop sleeps at most 200 ms between iterations and Wi-Fi polling
sleeps 250 ms, so the interrupt is serviced promptly; the program prints
`Stopped by user (Ctrl-C).` and returns to the REPL. A blocking DNS lookup may delay
the interrupt.

Configuration errors and, with Wi-Fi enabled, a missing `umqtt.simple` are reported on the USB console
before any connection attempt, then the program exits to the REPL.

### 6. Subscribe on the computer

```sh
mosquitto_sub -h <broker> -p 1883 -t 'noisenet/+/sound' -v
```

With broker authentication:

```sh
mosquitto_sub -h <broker> -p 1883 -u <mqtt-user> -P '<mqtt-password>' -t 'noisenet/+/sound' -v
```

A password on the command line is visible in shell history and the process list.
Prefer an options file readable only by the user, for example `~/.config/mosquitto_sub`
containing `-u <mqtt-user>` and `-P <mqtt-password>` with mode 600, which
`mosquitto_sub` reads automatically.

### 7. Check behaviour

See the acceptance checklist below.

## LoRaWAN uplink of per-minute maxima

With `LORA_ENABLED = True`, a Seeed Grove Wio-E5 (EU868, OTAA) transmits the maximum
sample level of every slot of `LORA_SLOT_S` seconds (default 60 s), batched
`LORA_SLOTS_PER_UPLINK` slots per uplink (default 5, i.e. one uplink every 5 minutes).
This works with or without Wi-Fi; in sensor-only mode it is the only network output.

### Wiring

| Wio-E5 Grove wire | Pico 2 W |
|---|---|
| RX (yellow) | GP0 / UART0 TX (pin 1) |
| TX (white) | GP1 / UART0 RX (pin 2) |
| VCC (red) | `3V3(OUT)` (pin 36) |
| GND (black) | GND |

Other pins are set with `LORA_UART_ID`, `LORA_TX_GPIO`, `LORA_RX_GPIO` (validated
against the RP2350 UART pin table). The antenna must be connected.

### Configuration (`secrets.py`)

```python
LORA_ENABLED = True
LORA_SLOT_S = 60             # one maximum per minute
LORA_SLOTS_PER_UPLINK = 5    # one uplink every 5 minutes
SAMPLE_RATE_HZ = 20          # the maximum is taken over individual samples
LORA_APP_KEY = None          # the Wio-E5 keeps the key programmed at the first join
```

The device must be registered on the network server (DevEUI and JoinEUI from
`AT+ID`, AppKey from the server). The AppKey is stored in the Wio-E5 flash; set
`LORA_APP_KEY` only to program a new key. It is never printed: the console shows
`<set>`/`<not set>`. `LORA_JOIN_EUI` replaces the module's AppEui when set.
`LORA_INTERVAL_S` from the previous version is rejected with an explanatory error.

### Behaviour

- At start-up the Wio-E5 is configured (`AT+MODE=LWOTAA`, `AT+DR=EU868`,
  `AT+CH=NUM,0-2`, `AT+PORT`, `AT+ADR`, `AT+DR=<LORA_DATA_RATE>`). After power-up the
  module may still be booting: `AT` is repeated every 0.5 s for up to 10 s. If it never
  answers, the sensor application continues and the configuration is retried every
  60 s (1 s probe, during which sampling pauses); batches that end meanwhile are logged
  as not sent (state `unconfigured`).
- Every Pico restart, including Ctrl-D and `make deploy`, leads to a new join: the
  start-up configuration resets the module's session, and the DevAddr changes.
- Slots are collected from start-up, joined or not. At the end of each batch the
  uplink is sent if the module is joined and idle; otherwise the batch is logged and
  dropped. No backlog is stored.
- Join and uplinks are non-blocking: the UART is polled from the main loop, so
  sampling continues during the uplink transaction.
- A failed join is retried after `LORA_JOIN_RETRY_S` (default 120 s). If the module
  reports `Please join network first`, the application joins again.
- Uplinks are unconfirmed; delivery is not acknowledged. The Wio-E5 enforces the
  EU868 duty cycle.
- Console: one `LoRa: slot k/N max … dBA estimate, n=…` line per slot, then
  `LoRa: uplink #n sent (N slots, highest … dBA estimate)`.

### Payload (format version 1, big-endian, FPort `LORA_PORT`, default 8)

Length 2N + 5 bytes for N slots (15 bytes for 5 slots):

| Bytes | Type | Content |
|---|---|---|
| 0 | uint8 | bits 7–4 format version (1), bits 3–0 slot count N (1–8) |
| 1 | uint8 | slot length in units of 10 s |
| 2 | uint8 | sequence number modulo 256 (resets on reboot) |
| 3 … 2N+2 | N × uint16 | slot maximum × 10 (dBA estimate), oldest slot first; `0xFFFF` = no sample |
| 2N+3 | uint8 | bit i: slot i contained an out-of-range sample (outside 0.6–2.6 V) |
| 2N+4 | uint8 | bit i: slot i had sample gaps (fewer than 90 % of `SAMPLE_RATE_HZ × LORA_SLOT_S`) or no sample |

Example: `15062C019A022B02D30384017E0000` = 5 slots of 60 s, sequence 44, maxima
41.0, 55.5, 72.3, 90.0, 38.2 dBA, no flags. The last slot ends just before
transmission: slot i ended about `(N − 1 − i) × slot` seconds before the uplink's
`received_at` time. The maximum is the highest per-sample estimate of a slot, not a
standards-compliant LAFmax; it depends on `SAMPLE_RATE_HZ`.

The TTN uplink payload formatter is `ttn_formatter.js` (Application → Payload
formatters → Uplink → Custom JavaScript formatter; paste the file content). For a
measurement uplink it returns:

```json
{"message": "slots", "sequence": 44, "slot_s": 60, "slots": [
  {"end_offset_s": -240, "max_db_a_estimate": 41.0, "out_of_range": false, "sample_gaps": false},
  ...
  {"end_offset_s": 0, "max_db_a_estimate": 38.2, "out_of_range": false, "sample_gaps": false}]}
```

The host tests run this formatter with Node.js (when installed) and compare it with
the Python reference decoder.

### Boot message

Once per start-up, right after the join (or `Joined already`), a 10-byte boot message
is sent on `LORA_BOOT_PORT` (default 9). It is sent once and never retried; the first
batch follows normally. `LORA_BOOT_MESSAGE = False` disables it.

Purpose: a Pico reset (`make deploy`, watchdog, Ctrl-D) does not power-cycle the
Wio-E5, which keeps its session and sends no join request. The boot message makes such
restarts visible on the server and records the configuration that produced the data.

| Byte(s) | Content |
|---|---|
| 0 | message format version (1) |
| 1–2 | firmware version major, minor (`APP_VERSION` in `main.py`, currently 1.0) |
| 3 | reset cause: 0 unknown, 1 power-on, 2 watchdog or `machine.reset()`, 3 other |
| 4 | slot length in units of 10 s |
| 5 | slots per uplink |
| 6–7 | uint16 sample rate × 100 (Hz) |
| 8 | flags: bit0 Wi-Fi enabled, bit1 calibration applied (`CAL_A`/`CAL_B` not default), bit2 ADR on |
| 9 | configured data rate 0–5, `0xFF` = module setting kept |

Example: `01010001060507D00005` = firmware 1.0, power-on, 60 s slots, 5 per uplink,
20 Hz, no flags, DR5. On rp2, `machine.reset()` (used by `mpremote reset` and therefore
`make deploy`) is implemented with the watchdog, so a deployment reports cause 2. A
soft reset (Ctrl-D) is not a hardware reset and is expected to report the cause of the preceding hardware reset; verify on the board.

The port is switched with `AT+PORT` before the boot message and restored before the
next batch. `ttn_formatter.js` selects the decoder by FPort (constants
`MEASUREMENT_PORT = 8`, `BOOT_PORT = 9` at the top of the file; keep them equal to the
configuration) and adds `"message": "boot"` or `"message": "slots"` to the output.
Airtime: 61.7 ms per boot at DR5.

### Airtime

| Configuration | Payload | Airtime per uplink (DR5, SF7) | Airtime per day |
|---|---|---|---|
| 5 slots of 60 s (default) | 15 bytes | 66.8 ms | about 19 s |
| 1 slot of 60 s | 7 bytes | about 57 ms | about 82 s |

The TTN Fair Use Policy allows 30 s of uplink airtime per device per day; the
default complies at DR5. At DR4 (SF8) the default uses about 36 s/day and exceeds it.
Join requests also count (about 1.5 s each at SF12). On a private network server only
the legal EU868 duty cycle applies. The start-up console prints the estimate and flags
configurations above 30 s/day.

## Network behaviour

- Wi-Fi association is bounded by `CONNECT_TIMEOUT_S` (default 15 s). Authentication
  failure and "access point not found" end the attempt early.
- MQTT socket connect, reads and writes are bounded by `SOCKET_TIMEOUT_S`
  (default 5 s). DNS resolution is not bounded.
- After a failed Wi-Fi or MQTT attempt, or any publish/socket error, the socket is
  closed and the next attempt waits `RETRY_DELAY_S` (default 5 s). No busy retry.
- Wi-Fi state is checked on every iteration. When the link is lost, the MQTT socket
  is closed and Wi-Fi is re-established before MQTT is reconnected.
- `PINGREQ` is sent when nothing was transmitted for half of `MQTT_KEEPALIVE_S`
  (default 60 s), independent of `PUBLISH_INTERVAL_S`. Incoming data (`PINGRESP`) is
  drained at least every 200 ms.
- When publication falls behind (slow network), missed slots are skipped instead of
  sent in a burst.
- A broker that disappears without closing the TCP connection is detected only when
  a write fails or the broker closes the session after its keepalive timeout
  (1.5 × `MQTT_KEEPALIVE_S`). Recovery then follows automatically.
- Recovery does not require unplugging the Pico. No hardware watchdog is enabled,
  so that Ctrl-C access from the REPL is preserved; a firmware lock-up still requires
  a reset.

## Troubleshooting

| Symptom | Checks |
|---|---|
| No serial port / drive in Thonny | Use a data cable; try another USB port; select interpreter "MicroPython (Raspberry Pi Pico)" |
| `No secrets.py with WIFI_SSID found` | Expected for sensor-only use; otherwise upload `secrets.py` to the Pico root (`make deploy`) |
| `make` reports `no device found` / `failed to access /dev/ttyACM0` | Close Thonny; use a data cable; check `dialout` group membership; set `PORT=` explicitly |
| `invalid configuration` | Read each listed key; placeholders `<...>` must be replaced |
| `umqtt.simple is not installed` | Install with method B, C or D; file must be `/lib/umqtt/simple.py` or `.mpy` |
| `Wi-Fi: failed (access point not found)` | 2.4 GHz band enabled; SSID spelling and case; distance to access point |
| `Wi-Fi: failed (authentication failed ...)` | `WIFI_PASSWORD`; WPA3-only networks may not be supported, use WPA2/WPA3 mixed mode |
| `MQTT: connection failed (OSError: ... EHOSTUNREACH/ECONNREFUSED ...)` | Broker address on the LAN (not `localhost`); Mosquitto listener on `0.0.0.0`; firewall on TCP 1883; client isolation on guest Wi-Fi |
| `MQTT: connection failed (MQTTException: 5)` | Not authorized: check `MQTT_USER`/`MQTT_PASSWORD` and broker ACL |
| Steady reading around 0.6–0.8 V that does not react to loud sound | The ADC input is probably floating: sensor on another analog port or pin. An unconnected input can read a steady, plausible voltage. Tie the configured GPIO to GND (expect about 0 V) to confirm which pin is read, then connect `A` to it or set `ADC_GPIO` to the port actually used |
| Messages arrive but levels do not change | Wiring of `A` to the configured `ADC_GPIO`; sensor powered from `3V3(OUT)`; common ground |
| `in_range: false` permanently | Output below 0.6 V (very quiet room or wiring fault) or above 2.6 V; check `ADC_REFERENCE_V` |
| Program does not start after power-up | File must be named exactly `main.py` at the Pico root |

## Host-side checks

From this directory, on a computer with Python 3:

```sh
python3 -m py_compile main.py sen0232.py config.example.py
python3 -m unittest discover -s tests -v
```

or `make check` and `make test`.

The tests replace `machine`, `network`, the clock and `umqtt.simple` with fakes. They
cover the conversion at 0.6/1.2/2.6 V (30/60/130 dBA), reference scaling,
calibration, out-of-range detection, configuration validation, tick wraparound,
Wi-Fi retry, MQTT connect and publish failure recovery, keepalive servicing with a
long interval, JSON fields, sampling-rate aggregation (energy average, min/max, interval boundaries, skipped late
samples), sensor-only mode, and absence of passwords in console output. They do not
prove MicroPython runtime compatibility or hardware behaviour.

## Manual hardware acceptance

Record the MicroPython version, broker host and date. Each item requires the
physical Pico, sensor and broker.

- [ ] Messages arrive on `noisenet/<device_id>/sound` about once per second; `sequence`
      increments by 1 without gaps under normal conditions.
- [ ] Quiet room, speech and a louder sound source produce clearly different
      `sound_level_db_a_estimate` values.
- [ ] Power cycle (unplug/replug) or Ctrl-D restarts the application; `sequence`
      restarts at 1 and `uptime_ms` restarts near 0.
- [ ] Switching the access point off for at least 30 s and back on: the console shows
      Wi-Fi loss and retries, then publishing resumes without touching the Pico.
- [ ] Stopping the broker (`sudo systemctl stop mosquitto`) for at least 30 s and
      restarting it: publishing resumes without touching the Pico.
- [ ] With `PUBLISH_INTERVAL_S = 120`, the broker does not drop the client (no
      keepalive timeout in the broker log) between publications.
- [ ] With `SAMPLE_RATE_HZ = 50` and `PUBLISH_INTERVAL_S = 1`, `sample_count` is close
      to 50 while connected.
- [ ] Cold start (unplug, wait 10 s, replug without a console): the gateway traffic shows
      the join request and the boot message within about 30 s.
- [ ] With `LORA_ENABLED = True`: the console shows `LoRa: network joined`, one
      `LoRa: slot k/5` line per minute and one `LoRa: uplink #n sent` every 5 minutes;
      the TTN device Live data shows `decoded_payload.slots` with 5 maxima and an
      increasing `sequence`.
- [ ] After `make deploy`, the TTN device Live data shows one uplink on FPort 9 with
      `"message": "boot"` and the expected firmware version and configuration.
- [ ] Ctrl-C returns to the REPL.
- [ ] Passwords do not appear in the console output.

## References

- SEN0232 specifications and pinout: <https://wiki.dfrobot.com/sen0232/>
- DFRobot voltage-to-dBA conversion: <https://wiki.dfrobot.com/sen0232/docs/18852>
- MicroPython RP2 quick reference (networking, ADC):
  <https://docs.micropython.org/en/latest/rp2/quickref.html>
- `umqtt.simple` source:
  <https://github.com/micropython/micropython-lib/tree/master/micropython/umqtt.simple>
