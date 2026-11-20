# NoiseNet Pico 2 W MQTT example: configuration template.
#
# 1. Copy this file to secrets.py (same directory).
# 2. Edit secrets.py only. With Wi-Fi enabled, replace every <...> placeholder.
# 3. Copy secrets.py to the root of the Pico filesystem with main.py.
#
# secrets.py is ignored by git (rule **/secrets.* in .gitignore).
# Never commit real Wi-Fi or MQTT credentials.

# --- Wi-Fi (2.4 GHz network; the Pico 2 W has no 5 GHz radio) ---
# WIFI_SSID = None selects sensor-only mode: readings are printed on the USB
# console, nothing is published, and the Wi-Fi/MQTT settings below are ignored.
# Set WIFI_SSID = "<your-ssid>" to enable Wi-Fi and MQTT.
WIFI_SSID = None
WIFI_PASSWORD = "<your-wifi-password>"  # "" for an open network
WIFI_COUNTRY = None  # optional two-letter regulatory code, e.g. "FR"

# --- MQTT broker (plain TCP, no TLS; trusted local network only) ---
# Hostname or IP address of the broker as seen from the Pico's Wi-Fi network.
# "localhost" would designate the Pico itself and is rejected.
# An IP address avoids DNS lookups, which have no timeout.
MQTT_BROKER = "<broker-ip-or-hostname>"
MQTT_PORT = 1883
MQTT_USER = None  # e.g. "noisenet" when the broker requires authentication
MQTT_PASSWORD = None  # requires MQTT_USER
MQTT_KEEPALIVE_S = 60  # 0 disables keepalive; otherwise > 2 * SOCKET_TIMEOUT_S

# --- Identity and topic ---
DEVICE_ID = None  # None: "pico2w-<hex of machine.unique_id()>"; no "/", "+", "#"
MQTT_TOPIC = None  # None: "noisenet/<device_id>/sound"

# --- Sensor (DFRobot SEN0232 analog output) ---
ADC_GPIO = 26  # GP26/ADC0 (allowed: 26, 27, 28)
ADC_REFERENCE_V = 3.3  # nominal; measure 3V3 or ADC_VREF for better accuracy

# Per-device linear correction: level = CAL_A * level_sensor + CAL_B.
# The defaults apply no correction: the assembled station is NOT calibrated.
CAL_A = 1.0
CAL_B = 0.0

# --- Timing (seconds) ---
PUBLISH_INTERVAL_S = 1.0  # one reading (MQTT message or console line) per interval
# ADC sampling rate, 0.01-200 Hz; SAMPLE_RATE_HZ * PUBLISH_INTERVAL_S must be >= 1.
# Each reading summarizes the samples of its interval: energy-averaged level,
# min, max, sample count. 1.0 with a 1 s interval gives one sample per reading.
SAMPLE_RATE_HZ = 1.0
CONNECT_TIMEOUT_S = 15  # bound for one Wi-Fi association attempt
SOCKET_TIMEOUT_S = 5  # bound for MQTT socket connect, read and write
RETRY_DELAY_S = 5  # wait after a failed Wi-Fi or MQTT attempt

# --- LoRaWAN uplink through a Seeed Wio-E5 (EU868, OTAA) ---
# The maximum sample level of every LORA_SLOT_S slot is kept; every
# LORA_SLOTS_PER_UPLINK slots, one binary uplink carries them all (see README.md).
# Default: one maximum per minute, one 15-byte uplink every 5 minutes, about
# 19 s/day of airtime at DR5 (TTN Fair Use Policy: 30 s/day per device).
# Independent of Wi-Fi/MQTT.
LORA_ENABLED = False
LORA_UART_ID = 0  # UART0: TX GP0 / RX GP1 (pins 1 and 2)
LORA_TX_GPIO = 0  # Pico TX -> Wio-E5 RX
LORA_RX_GPIO = 1  # Pico RX <- Wio-E5 TX
LORA_SLOT_S = 60  # seconds per maximum; multiple of 10, 10-2550
LORA_SLOTS_PER_UPLINK = 5  # maxima per uplink, 1-8
LORA_PORT = 8  # LoRaWAN FPort of the measurement uplinks, 1-223
LORA_BOOT_MESSAGE = True  # one 10-byte message per start-up (version, reset cause)
LORA_BOOT_PORT = 9  # FPort of the boot message; must differ from LORA_PORT
LORA_DATA_RATE = 5  # EU868 DR0 (SF12) .. DR5 (SF7); None keeps the module setting
LORA_ADR = False  # True lets the network server choose the data rate
LORA_JOIN_RETRY_S = 120  # wait after a failed join
# The Wio-E5 stores the AppKey in its flash. Set it here only to (re)program it.
LORA_APP_KEY = None  # "<32 hex digits from the network server>"; secret
LORA_JOIN_EUI = None  # "<16 hex digits>"; None keeps the module's AppEui
