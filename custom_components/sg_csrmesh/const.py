"""Constants for SG Smart Gen1 (CSRmesh)."""

from __future__ import annotations

DOMAIN = "sg_csrmesh"

CONF_PASSPHRASE = "passphrase"
CONF_NETWORK_KEY = "network_key"      # optional raw 16-byte hex key
CONF_DEVICES = "devices"              # options: {"1a2b": {"name":..,"kind":..}}
CONF_MESH_ID = "mesh_id"
CONF_KIND = "kind"
CONF_POLL_INTERVAL = "poll_interval"
CONF_MIN_LEVEL = "min_level"
CONF_TAG = "tag"                      # per device: SG tag, hex ("e7a4"/"0000")
CONF_MODEL = "model"                  # per device: SG type name, see models.py
CONF_PANEL = "panel"                  # per Smart Switch: two_rockers / one_rocker
CONF_PAIRS = "pairs"                  # per Smart Switch: {rocker: target hex}, HA's record
CONF_FADING = "fading"                # per dimmer: last start-up fading sent (1/10 s)
CONF_SPEED = "dimming_speed"          # per dimmer: last dimming speed sent (0/1)
CONF_PUSH = "push_input"              # per dimmer: last push-button input sent (0/1)

DEFAULT_PASSPHRASE = "1234"
DEFAULT_POLL_INTERVAL = 300           # s; dimmers do not push changes
DEFAULT_MIN_LEVEL = 10                # %; SG dimmers have a ~10 % floor
THERMOSTAT_POLL = 60                  # s; thermostats are read this often
FIRMWARE_DELAY = 15                   # s after start before firmware is read

STATUS_LISTEN_TIME = 4.0
CAPTURE_BUFFER = 2000

SIGNAL_UPDATE = f"{DOMAIN}_update"
SIGNAL_PAIRINGS = f"{DOMAIN}_pairings"
EVENT_FRAME = f"{DOMAIN}_frame"

SERVICE_CAPTURE = "capture"
