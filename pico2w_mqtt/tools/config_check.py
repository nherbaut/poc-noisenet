"""Host-side check of secrets.py, used by the Makefile.

    python3 tools/config_check.py [secrets.py]          validate, print mode
    python3 tools/config_check.py --wifi [secrets.py]   exit 0 if Wi-Fi enabled

Secret values are never printed.
"""

import os
import runpy
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import main  # noqa: E402


def load(path):
    namespace = runpy.run_path(path)
    cfg = dict(main.DEFAULTS)
    for key in main.DEFAULTS:
        if key in namespace:
            cfg[key] = namespace[key]
    return cfg


def run(argv):
    wifi_only = "--wifi" in argv
    args = [a for a in argv if a != "--wifi"]
    path = args[0] if args else os.path.join(ROOT, "secrets.py")
    cfg = load(path)
    if wifi_only:
        return 0 if main.wifi_enabled(cfg) else 1
    errors = main.validate_config(cfg)
    if errors:
        print("Invalid configuration in %s:" % path)
        for error in errors:
            print("  - " + error)
        return 1
    if main.wifi_enabled(cfg):
        print("%s: valid, Wi-Fi/MQTT mode" % path)
    else:
        print("%s: valid, sensor-only mode (WIFI_SSID = None)" % path)
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
