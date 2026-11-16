"""Read .env or interactively select and save a host IPv4 address."""

import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
BIND_LINE = re.compile(r"^\s*(?:export\s+)?BIND_IP\s*=(.*)$")


def read_bind_ip(text):
    value = ""
    for line in text.splitlines():
        match = BIND_LINE.match(line)
        if match:
            value = match.group(1).strip()
            if value.startswith(("'", '"')):
                quote = value[0]
                end = value.find(quote, 1)
                if end < 0 or (value[end + 1:].strip() and not value[end + 1:].strip().startswith("#")):
                    raise ValueError("Invalid quoted BIND_IP in .env")
                value = value[1:end]
            else:
                value = value.split("#", 1)[0].strip()
    if value:
        address = ipaddress.IPv4Address(value)
        if address.is_unspecified or address.is_multicast or str(address) == "255.255.255.255":
            raise ValueError("BIND_IP must be a specific local IPv4 address, such as 127.0.0.1")
    return value


def local_addresses():
    addresses = {}
    try:
        result = subprocess.run(
            ["ip", "-j", "-4", "addr", "show", "up"],
            check=True, capture_output=True, text=True,
        )
        for interface in json.loads(result.stdout):
            for entry in interface.get("addr_info", []):
                if entry.get("family") == "inet":
                    addresses.setdefault(entry["local"], interface["ifname"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        # hostname is a fallback for hosts without iproute2 (including macOS).
        try:
            result = subprocess.run(["hostname", "-I"], check=True, capture_output=True, text=True)
            for value in result.stdout.split():
                try:
                    address = ipaddress.IPv4Address(value)
                except ValueError:
                    continue
                addresses.setdefault(str(address), "host")
        except (OSError, subprocess.SubprocessError):
            pass
    addresses.setdefault("127.0.0.1", "loopback; local access only")
    return list(addresses.items())


def save_bind_ip(path, text, address):
    lines = text.splitlines(keepends=True)
    # Replace every active assignment, preventing a later duplicate from winning.
    replaced = False
    for index, line in enumerate(lines):
        if BIND_LINE.match(line):
            lines[index] = f"BIND_IP={address}\n"
            replaced = True
    if not replaced:
        if text and not text.endswith("\n"):
            lines.append("\n")
        lines.append(f"BIND_IP={address}\n")
    path.write_text("".join(lines))


def ensure_bind_ip(path=ENV_FILE, stdin=None, stderr=None):
    stdin = sys.stdin if stdin is None else stdin
    stderr = sys.stderr if stderr is None else stderr
    text = path.read_text() if path.exists() else ""
    address = read_bind_ip(text)
    if address:
        return address
    if not stdin.isatty():
        raise ValueError("BIND_IP is missing from .env. Run make configure in a terminal, or set BIND_IP=<local-ip> in .env.")

    addresses = local_addresses()
    print("Choose the local IPv4 address for MQTT, WebSockets, and MQTTX Web:", file=stderr)
    for index, (address, interface) in enumerate(addresses, start=1):
        print(f"  {index}) {address} ({interface})", file=stderr)
    while True:
        print(f"Select [1-{len(addresses)}]: ", end="", file=stderr, flush=True)
        choice = stdin.readline()
        if not choice:
            raise ValueError("No IP selected; .env was not changed")
        try:
            index = int(choice.strip()) - 1
        except ValueError:
            index = -1
        if 0 <= index < len(addresses):
            address = addresses[index][0]
            save_bind_ip(path, text, address)
            print(f"Saved BIND_IP={address} to {path.name}", file=stderr)
            return address
        print("Enter one of the listed numbers.", file=stderr)


def main():
    try:
        print(ensure_bind_ip())
        return 0
    except (OSError, ValueError) as exc:
        print(f"Bind IP configuration: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nNo IP selected; .env was not changed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
