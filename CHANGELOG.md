# Changelog

## 0.7.0rc1 – 2026-10-19

- **Model at setup.** Adding a device asks for its SG model; a device moved from the gateway takes it from
  HomeKit. The model decides which settings it gets. Devices added before 0.7.0 get a repair asking for
  it; **Edit a device** in the options changes it.
- **Dimmer settings** under Configuration, as in the 3.0 integration: dimmer minimum, start-up and
  maximum level (read from the dimmer and read back after a change), fading and dimming speed, and
  push-button input (show the value HA last sent; the dimmer cannot be read). An unacknowledged setting
  raises an error instead of being shown as done.
- **Smart Switch panel:** two rockers (four button entities) or one big rocker (top and bottom), chosen
  at setup. Changing the panel removes the old button entities.
- **Pair a Smart Switch with a light** from the options: the switch stores it and controls the light
  without HA. Sent while the switch is awake after a press (wake, clear, set, sleep); anything not
  acknowledged goes out at the next press. «Styrer» on the switch and «Styres av» on the light show HA's
  record. Only lights with the switch's tag are offered (a switch cannot control a light set up the
  other way, verified on hardware).
- **Battery** sensor for Smart Switches (sent with every press, restored after a restart).
- **HeatReg Smart:** set-point and on/off, current temperature (floor), floor and room temperature as
  sensors, heating/idle. Read every 60 s. Verified on three thermostats.
- Firmware read from Gen4 devices (and from HeatReg status) and shown on the device page.
- Switches are no longer polled (they sleep). Event attribute `button` is now the button's key
  (`top_left` …) instead of its number.

## 0.6.3rc1 – 2026-10-19

- `send_on_press` takes `repeats` and `gap`. With `repeats: 1, gap: 0` a switch configuration
  (wake, clear, set, sleep) fits in the switch's ~2 s wake window, as the SG app does it. No pause
  after the last message, and the log shows each message's time after the press.

- `capture` calls that overlap no longer cut each other short (the first one to end used to switch
  logging off for the one still running).
- Trim read and set (max) verified on hardware; see docs/gen1-protocol.md 6.1.
- Options flow keeps every existing option when saving (it used to copy a fixed list of keys, which
  would have deleted any option added later).
- `csrmesh/settings.py`: builders and parsers for trim (`FF29`/`FF2A`/`FF2B`), mode (`FF39`),
  push-button input (`FF47`), fading and dimming speed (`FF48`), firmware (`FF5E`) and the
  acknowledgement `FF08`, plus the app's trim level tables. Mapped from the SG app, **not verified on
  hardware** and not used by the integration yet.
- Tests for v0.6.2 (protocol, settings, config/options flow, light, switch), `pyproject.toml`,
  `requirements-test.txt`, `CLAUDE.md`, `docs/gen1-protocol.md`.

## 0.6.2 – 2026-09-28

First version in git. Lights (dimmer, on/off), Smart Switch buttons as events, status polling and
reports made by hand (`FF50`/`FF88`), move devices from the SG gateway (HomeKit), add new devices
(MASP via recsrmesh), add by address, remove (optionally factory reset), settings (poll interval,
minimum level), capture/send/send_on_press tools, Norwegian translation.
