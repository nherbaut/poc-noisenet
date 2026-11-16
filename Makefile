.DEFAULT_GOAL := up

COMPOSE ?= docker compose
PYTHON ?= python3

.PHONY: up down build restart logs ps configure topology help

define compose
	@bind_ip="$$($(PYTHON) scripts/bind_ip.py)" && BIND_IP="$$bind_ip" $(COMPOSE) $(1)
endef

up:
	$(call compose,up --build -d)
	@$(MAKE) --no-print-directory topology

down:
	$(call compose,down)

build:
	$(call compose,build)

restart:
	$(call compose,up --build -d --force-recreate)
	@$(MAKE) --no-print-directory topology

logs:
	$(call compose,logs --follow --tail=100)

ps:
	$(call compose,ps)

configure:
	@bind_ip="$$($(PYTHON) scripts/bind_ip.py)" && printf 'Using BIND_IP=%s from .env\n' "$$bind_ip"

define TOPOLOGY
NoiseNet MQTT topology

  pico-client ---- MQTT ----> NanoMQ ---- MQTT ----> pi-server
  temperature every 10s        ^                    logs readings
                              |
                 +------------+-------------+
                 |                          |
           External services          MQTTX Web
           @BIND_IP@:1883       http://@BIND_IP@:8080
                                     WebSocket
                                 @BIND_IP@:8083/mqtt

Broker listeners:
  MQTT:       @BIND_IP@:1883
  WebSocket:  @BIND_IP@:8083/mqtt
  MQTTX Web:  @BIND_IP@:8080

Publish:   noisenet/pico-01/environment/temperature
Subscribe: noisenet/+/environment/temperature
View all:  noisenet/#

Viewer URL: http://@BIND_IP@:8080
Viewer connection: ws://@BIND_IP@:8083/mqtt
Viewer preset: NoiseNet / noisenet/# (click Connect)
Logs: make logs
Stop: make down
endef
export TOPOLOGY

topology:
	@bind_ip="$$($(PYTHON) scripts/bind_ip.py)" && printf '%s\n' "$$TOPOLOGY" | sed "s/@BIND_IP@/$$bind_ip/g"

help:
	@printf '%s\n' \
	  'make / make up  Build and start the stack, then show its topology' \
	  'make down       Stop and remove the stack' \
	  'make build      Build project images' \
	  'make restart    Rebuild/recreate containers using .env and show the topology' \
	  'make logs       Follow container logs (Ctrl-C to exit)' \
	  'make ps         Show container status' \
	  'make topology   Print the default topology without using Docker' \
	  'make configure  Read .env or select and save a local bind IP' \
	  'make help       Show these commands' \
	  'Edit BIND_IP in .env to change the host interface; then run make up'
