# NoiseNet MQTT demo

Requires Docker Engine with Docker Compose v2 or newer, GNU Make, and Python 3.
Linux interface discovery uses `ip` (iproute2), with `hostname -I` as a fallback.

```sh
make                 # Build and start all four containers; print the topology
make configure       # Select and save a bind IP, if .env has none
make logs            # Follow broker, publisher, subscriber, and viewer logs
make ps              # Show container status
make topology        # Print the default topology without contacting Docker
make down            # Stop and remove the project containers and network
```

On the first run, Make lists your local IPv4 addresses with interface names and
asks which one to use. It saves your selection as `BIND_IP` in `.env`, preserving
any other settings. Subsequent runs use that address without prompting.
Choose your LAN/Wi-Fi address for access from other machines or `127.0.0.1`
for access from this machine only. Docker/VM bridge addresses are also listed.

To configure without a prompt, copy `.env.example` to `.env` and set a concrete
local IPv4 address, for example `BIND_IP=192.168.88.254`. If no address is saved
and stdin is not a terminal (for example CI), Make exits with instructions rather
than picking an interface. An invalid configured address also causes an error.
The `.env` file is ignored by Git. To choose again, clear its `BIND_IP` value and
run `make configure`.

`make build` builds the Python and viewer images. `make restart` rebuilds and
recreates containers so changes to `BIND_IP` take effect.
After changing code or configuration, run `make up` to rebuild/recreate services.
`make help` lists all targets. Override the Compose command if needed, for example
`make COMPOSE='docker --context my-context compose'`.

## Data flow

`pico-client` simulates a temperature between 18 and 28 degrees Celsius, publishes
immediately after connecting, then every 10 seconds. `pi-server` logs readings to
stdout. Both use Python and Paho MQTT, retry unavailable brokers, reconnect after
disconnection, and shut down on SIGTERM. The subscriber resubscribes on reconnect
and logs malformed payloads without stopping.

Publish topic: `noisenet/pico-01/environment/temperature`.
Subscriber filter: `noisenet/+/environment/temperature`.
Use `noisenet/#` to view every NoiseNet topic. `#` is a subscription wildcard and
must be the last level; `#/noisenet` is invalid. Published topics have no wildcards.

Example JSON payload:

```json
{"device_id":"pico-01","timestamp":"2026-11-15T20:00:00+00:00","temperature_c":22.5}
```

MQTT 3.1.1 and QoS 1 are used. Duplicate deliveries may be logged. Each reading
is retained so new subscribers receive the latest temperature immediately.
There is no database or durable message history; retained data may be lost when
the broker restarts and is republished by the client on its next interval.

## Browser message viewer

1. Open the `Viewer URL` printed by Make: `http://<host-ip>:8080` (MQTTX Web).
2. Select the preconfigured `NoiseNet` connection and click **Connect**.
3. The saved `noisenet/#` subscription (QoS 1) receives the JSON readings.

The viewer image extends MQTTX Web v1.13.2 with a first-visit preset in
`mqtt-viewer/defaults.js`: MQTT 3.1.1, anonymous WebSockets on port 8083 at `/mqtt`,
and a unique client ID. Its broker hostname comes from the address used to open
the viewer, so it works through a host IP, DNS name, or localhost.

The preset is added once per browser origin (host and port), including browsers
that already have other saved connections. Existing `NoiseNet` connections,
settings, and subsequent edits or deletions are preserved. Reload the viewer
after updating the image. To restore a deleted preset, clear the viewer's site
storage and reload; this also removes other saved MQTTX data for that origin.

The viewer runs in your browser and uses the host WebSocket endpoint, not the
Compose hostname `nanomq`. MQTTX Web is a general MQTT client for viewing, subscribing,
and publishing messages, rather than a historical dashboard.

## External connections

NanoMQ listens on `0.0.0.0` inside its container. Compose publishes MQTT port 1883,
WebSocket port 8083, and viewer port 8080 on the **host IP selected in `.env`**:

| Client | Endpoint |
| --- | --- |
| Python containers | `nanomq:1883` |
| Host or external MQTT clients | `<BIND_IP>:1883` |
| Host or external WebSocket clients | `ws://<BIND_IP>:8083/mqtt` |
| Web viewer | `http://<BIND_IP>:8080` |

Use the selected address even from the Docker host: a service bound to a LAN IP
does not also listen on `localhost`. Host firewall and network rules must allow
the selected ports. Edit `BIND_IP` in `.env`, then run `make up` or `make restart`
to change all host bindings and the printed URLs together. Containers continue
communicating over their Compose network. `HOST_IP` is no longer used.

If startup reports `address already in use`, free the conflicting host port or
change the host side of its `ports` mapping in `docker-compose.yaml`. For example,
`${BIND_IP}:11883:1883` exposes MQTT on host port 11883 while the Python containers
continue using `nanomq:1883`. The printed topology describes the default ports.

This development broker permits anonymous access and uses no TLS. Every client
that can reach the published ports can publish and subscribe.

## MikroTik gateway: log subscribed messages

See the [MikroTik scripting documentation](https://manual.mikrotik.com/docs/developer-guides/scripting/)
and [MQTT subscription/on-message reference](https://help.mikrotik.com/docs/spaces/ROS/pages/46759978/MQTT).
Use RouterOS **7.12 or later** with the **iot** package installed.

Run this example in the gateway's RouterOS terminal. Replace `192.168.88.254`
with the Docker host's reachable `BIND_IP` from `.env` (not `127.0.0.1`).
If your gateway already has the broker and `noisenet/#` subscription, skip the
first two commands and replace `noisenet` with your existing broker name below.
Use a distinct client ID on each gateway.

```routeros
# One-time broker and subscription setup.
/iot mqtt brokers add name="noisenet" address="192.168.88.254" port=1883 ssl=no client-id="mikrotik-noisenet" auto-connect=yes
/iot mqtt subscribe broker="noisenet" topic="noisenet/#" qos=1

# Log the actual topic and JSON payload whenever a message arrives.
/iot mqtt subscriptions set [find where broker="noisenet" and topic="noisenet/#"] on-message={
    :log info ("NoiseNet MQTT topic=" . $msgTopic . " payload=" . $msgData)
}

# Re-establish the connection after configuring the subscription.
/iot mqtt disconnect broker="noisenet"
/iot mqtt connect broker="noisenet"

# Follow matching gateway log entries; press Ctrl-C to stop following.
/log print follow where message~"NoiseNet MQTT"
```

Example log entry:

```text
21:15:00 script,info NoiseNet MQTT topic=noisenet/pico-01/environment/temperature payload={"device_id":"pico-01","timestamp":"2026-11-15T21:15:00+00:00","temperature_c":22.5}
```

Keep `$msgTopic` and `$msgData` inside the inline `on-message` handler; RouterOS
does not pass them into a separate `/system script run` script. Inspect the
connection with `/iot mqtt brokers print` (`connected=yes`) and received messages
with `/iot mqtt subscriptions recv print`. New temperatures arrive every 10 seconds.

## Configuration

Host binding is configured by `BIND_IP` in `.env`; direct `docker compose`
commands also read this file and require that value. The Makefile passes the
saved address explicitly, so it takes precedence over an exported `BIND_IP`.
For a remote Docker context, set an address that belongs to that remote host.

Edit the service environment in `docker-compose.yaml`, then run `make up`:

| Variable | Service | Default |
| --- | --- | --- |
| `MQTT_HOST` | Both Python services | `nanomq` |
| `MQTT_PORT` | Both Python services | `1883` |
| `DEVICE_ID` | pico-client | `pico-01` |
| `PUBLISH_INTERVAL_SECONDS` | pico-client | `10` |
| `MQTT_TOPIC_FILTER` | pi-server | `noisenet/+/environment/temperature` |

Give each publisher a distinct device ID when running multiple clients. The
topology printed by Make uses your selected bind IP and the default ports/topics.
Run `python3 -m unittest discover -s tests -v` to check IP configuration behavior.

## Checking the demo

- Run `make up` twice; both commands should succeed without duplicate services.
- Run `make logs`; confirm `pi-server` receives a reading about every 10 seconds.
- Connect MQTTX Web and confirm it receives a retained value and subsequent updates.
- From another machine, connect an MQTT client to the Docker host's IP on port 1883.
- Run `docker compose restart nanomq`; both Python services should reconnect and
  the server should receive readings again.
- Publish `not-json` to `noisenet/test/environment/temperature` in MQTTX Web;
  confirm `pi-server` logs a malformed-reading warning and continues receiving.
- Run `make down`; the project containers and network should be removed.

Upstream references: [NanoMQ listeners](https://nanomq.io/docs/en/latest/config-description/listener.html),
[MQTTX Web setup](https://mqttx.app/docs/web/get-started), and
[MQTT topic filters](https://docs.oasis-open.org/mqtt/mqtt/v3.1.1/os/mqtt-v3.1.1-os.html).
