# NoiseNet Pico 2 W MQTT example: deployment over USB serial with mpremote.
#
#   make deploy            copy the application to the Pico and restart it
#   make debug             open the MicroPython REPL / console of the Pico
#   make deploy PORT=/dev/ttyACM0
#
# Run "make help" for all targets.

PYTHON   ?= python3
MPREMOTE ?= mpremote
PORT     ?= auto
UMQTT    ?= 1

MPR       := $(MPREMOTE) connect $(PORT)
APP_FILES := sen0232.py wioe5.py main.py
SECRETS   := secrets.py

export PYTHONDONTWRITEBYTECODE := 1

.PHONY: help tools check test deploy umqtt run debug reset ls

help:
	@echo "Targets:"
	@echo "  tools   install mpremote on this computer"
	@echo "  check   compile sources and validate $(SECRETS) on this computer"
	@echo "  test    run host-side unit tests (no hardware)"
	@echo "  deploy  copy $(APP_FILES) and $(SECRETS) to the Pico, install"
	@echo "          umqtt.simple when Wi-Fi is enabled, then reset the Pico"
	@echo "  umqtt   install umqtt.simple on the Pico (downloaded by this computer)"
	@echo "  run     run main.py from this computer without saving it on the Pico"
	@echo "  debug   open the Pico REPL (Ctrl-C: interrupt, Ctrl-D: soft reboot,"
	@echo "          Ctrl-] or Ctrl-X: quit)"
	@echo "  reset   hard-reset the Pico (restarts main.py)"
	@echo "  ls      list files on the Pico"
	@echo "Variables: PORT=$(PORT) (e.g. /dev/ttyACM0), UMQTT=$(UMQTT) (0: skip umqtt install)"

tools:
	pipx install mpremote || $(PYTHON) -m pip install --user mpremote

# Without secrets.py, start from the template (sensor-only: WIFI_SSID = None).
$(SECRETS):
	cp config.example.py $@
	@echo "Created $@ from config.example.py (sensor-only mode)."
	@echo "Edit $@ and set WIFI_SSID to enable Wi-Fi/MQTT."

check: $(SECRETS)
	$(PYTHON) -m py_compile $(APP_FILES) config.example.py
	$(PYTHON) tools/config_check.py $(SECRETS)

test:
	$(PYTHON) -m unittest discover -s tests -v

deploy: check
	@if [ "$(UMQTT)" != "0" ] && $(PYTHON) tools/config_check.py --wifi $(SECRETS); then \
		echo "Wi-Fi enabled: installing umqtt.simple"; \
		$(MPR) mip install umqtt.simple; \
	fi
	$(MPR) cp $(SECRETS) $(APP_FILES) : + reset
	@echo "Deployed. The Pico restarts main.py; run 'make debug' to see the console."

umqtt:
	$(MPR) mip install umqtt.simple

run: check
	$(MPR) cp $(SECRETS) sen0232.py wioe5.py : + run main.py

debug:
	@echo "Pico REPL: Ctrl-C interrupts main.py, Ctrl-D soft-reboots (runs main.py), Ctrl-] or Ctrl-X quits."
	$(MPR) repl

reset:
	$(MPR) reset

ls:
	$(MPR) fs ls :
	-$(MPR) fs ls :lib/umqtt
